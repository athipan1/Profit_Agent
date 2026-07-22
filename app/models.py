from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import math
import os
import re
from typing import Any, Dict, Generic, List, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator


T = TypeVar("T")

RISK_MISMATCH_POLICIES = {"reject", "warn", "recalculate"}
RISK_MISMATCH_REL_TOL = 1e-6
RISK_MISMATCH_ABS_TOL = 1e-6
SYMBOL_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9.-]{0,14}$")
POSITION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,199}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        str_strip_whitespace=True,
    )


def _risk_mismatch_policy() -> str:
    policy = os.getenv("PROFIT_RISK_MISMATCH_POLICY", "reject").strip().lower()
    if policy not in RISK_MISMATCH_POLICIES:
        supported = ", ".join(sorted(RISK_MISMATCH_POLICIES))
        raise ValueError(
            f"PROFIT_RISK_MISMATCH_POLICY must be one of: {supported}"
        )
    return policy


class ProfitAction(str, Enum):
    HOLD = "hold"
    MOVE_STOP = "move_stop"
    PARTIAL_EXIT = "partial_exit"
    EXIT_ALL = "exit_all"
    REVIEW = "review"


class PositionSide(str, Enum):
    LONG = "long"


class ProfitPosition(StrictModel):
    symbol: str
    side: PositionSide = PositionSide.LONG
    quantity: float = Field(gt=0)
    entry_price: float = Field(gt=0)
    current_price: float = Field(gt=0)
    stop_loss: Optional[float] = Field(default=None, gt=0)
    highest_price_since_entry: Optional[float] = Field(default=None, gt=0)
    risk_per_share: Optional[float] = Field(default=None, gt=0)
    unrealized_pl_pct: Optional[float] = None
    strategy_bucket: Optional[str] = None

    _highest_price_since_entry_inferred: bool = PrivateAttr(default=False)
    _risk_per_share_warning: Optional[str] = PrivateAttr(default=None)

    @field_validator("symbol", mode="before")
    @classmethod
    def validate_symbol(cls, value: Any) -> Any:
        if not isinstance(value, str):
            raise ValueError("symbol must be a string")
        if value != value.strip():
            raise ValueError("symbol must not contain leading or trailing whitespace")
        if not value or not SYMBOL_PATTERN.fullmatch(value):
            raise ValueError(
                "symbol must start with a letter and contain only letters, digits, '.' or '-'"
            )
        return value

    @model_validator(mode="after")
    def validate_long_position(self) -> "ProfitPosition":
        if self.stop_loss is not None and self.stop_loss >= self.entry_price:
            raise ValueError("stop_loss must be below entry_price for a long position")

        if self.highest_price_since_entry is None:
            self.highest_price_since_entry = max(self.entry_price, self.current_price)
            self._highest_price_since_entry_inferred = True
        elif self.highest_price_since_entry < self.entry_price:
            raise ValueError("highest_price_since_entry must be at least entry_price")
        elif self.highest_price_since_entry < self.current_price:
            raise ValueError("highest_price_since_entry must be at least current_price")

        if self.stop_loss is not None:
            expected_risk = self.entry_price - self.stop_loss
            if self.risk_per_share is None:
                self.risk_per_share = expected_risk
            elif not math.isclose(
                self.risk_per_share,
                expected_risk,
                rel_tol=RISK_MISMATCH_REL_TOL,
                abs_tol=RISK_MISMATCH_ABS_TOL,
            ):
                policy = _risk_mismatch_policy()
                message = (
                    "risk_per_share does not match entry_price - stop_loss; "
                    f"received {self.risk_per_share:g}, expected {expected_risk:g}"
                )
                if policy == "reject":
                    raise ValueError(message)
                self._risk_per_share_warning = f"{message}; policy={policy}"
                if policy == "recalculate":
                    self.risk_per_share = expected_risk

        if self.unrealized_pl_pct is None:
            self.unrealized_pl_pct = (self.current_price - self.entry_price) / self.entry_price
        return self

    @property
    def highest_price_since_entry_inferred(self) -> bool:
        """Whether the service had to infer the peak because the caller omitted it."""
        return self._highest_price_since_entry_inferred

    @property
    def risk_per_share_warning(self) -> Optional[str]:
        return self._risk_per_share_warning


class ProfitLifecycle(StrictModel):
    """Database-owned state for one open-position lifecycle."""

    position_id: str
    position_version: int = Field(ge=1)
    first_target_executed: bool = False
    second_target_executed: bool = False
    total_exited_quantity: float = Field(default=0, ge=0)
    remaining_quantity: float = Field(gt=0)

    @field_validator("position_id", mode="before")
    @classmethod
    def validate_position_id(cls, value: Any) -> Any:
        if not isinstance(value, str):
            raise ValueError("position_id must be a string")
        if value != value.strip() or not POSITION_ID_PATTERN.fullmatch(value):
            raise ValueError("position_id has an invalid format")
        return value

    @model_validator(mode="after")
    def validate_target_sequence(self) -> "ProfitLifecycle":
        if self.second_target_executed and not self.first_target_executed:
            raise ValueError(
                "second_target_executed requires first_target_executed"
            )
        return self


class ProfitPlanRequest(StrictModel):
    position: ProfitPosition
    lifecycle: Optional[ProfitLifecycle] = None
    first_take_profit_r: float = Field(default=2.0, gt=0)
    second_take_profit_r: float = Field(default=3.0, gt=0)
    partial_exit_pct: float = Field(default=0.30, gt=0, le=1)
    trailing_stop_pct: float = Field(default=0.08, gt=0, le=1)
    break_even_trigger_r: float = Field(default=1.0, gt=0)
    exit_on_stop_breach: bool = True
    warnings: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_target_ordering(self) -> "ProfitPlanRequest":
        if self.second_take_profit_r <= self.first_take_profit_r:
            raise ValueError("second_take_profit_r must be greater than first_take_profit_r")
        if self.lifecycle is not None and not math.isclose(
            self.lifecycle.remaining_quantity,
            self.position.quantity,
            rel_tol=RISK_MISMATCH_REL_TOL,
            abs_tol=RISK_MISMATCH_ABS_TOL,
        ):
            raise ValueError(
                "lifecycle.remaining_quantity must match position.quantity"
            )
        return self


class ProfitActionItem(StrictModel):
    action: ProfitAction
    symbol: str
    quantity: float = Field(ge=0)
    recommended_stop: Optional[float] = None
    reason: str
    confidence_score: float = Field(ge=0, le=1)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ProfitPlanData(StrictModel):
    symbol: str
    current_r_multiple: Optional[float]
    unrealized_pl_pct: float
    primary_action: ProfitAction
    actions: List[ProfitActionItem]
    warnings: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    trigger: Optional[str] = None
    recommended_stop: Optional[float] = None
    requires_risk_approval: bool = False
    advisory_only: bool = True
    decision_id: Optional[str] = None
    decision_type: Optional[str] = None
    position_version: Optional[int] = None
    next_lifecycle_state: Dict[str, bool] = Field(default_factory=dict)


class HealthData(StrictModel):
    status: str = "healthy"
    service: str = "profit-agent"


class StandardAgentResponse(StrictModel, Generic[T]):
    status: str
    agent_type: str = "profit-agent"
    version: str = "0.1.0"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    data: Optional[T] = None
    error: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
