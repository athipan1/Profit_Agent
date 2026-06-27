from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field, model_validator


T = TypeVar("T")


class ProfitAction(str, Enum):
    HOLD = "hold"
    MOVE_STOP = "move_stop"
    PARTIAL_EXIT = "partial_exit"
    EXIT_ALL = "exit_all"
    REVIEW = "review"


class PositionSide(str, Enum):
    LONG = "long"


class ProfitPosition(BaseModel):
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

    @model_validator(mode="after")
    def infer_risk_per_share(self) -> "ProfitPosition":
        if self.risk_per_share is None and self.stop_loss is not None and self.entry_price > self.stop_loss:
            self.risk_per_share = self.entry_price - self.stop_loss
        if self.highest_price_since_entry is None:
            self.highest_price_since_entry = max(self.entry_price, self.current_price)
        if self.unrealized_pl_pct is None:
            self.unrealized_pl_pct = (self.current_price - self.entry_price) / self.entry_price
        return self


class ProfitPlanRequest(BaseModel):
    position: ProfitPosition
    first_take_profit_r: float = Field(default=2.0, gt=0)
    second_take_profit_r: float = Field(default=3.0, gt=0)
    partial_exit_pct: float = Field(default=0.30, gt=0, le=1)
    trailing_stop_pct: float = Field(default=0.08, gt=0, le=1)
    break_even_trigger_r: float = Field(default=1.0, gt=0)
    exit_on_stop_breach: bool = True


class ProfitActionItem(BaseModel):
    action: ProfitAction
    symbol: str
    quantity: float = Field(ge=0)
    recommended_stop: Optional[float] = None
    reason: str
    confidence_score: float = Field(ge=0, le=1)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ProfitPlanData(BaseModel):
    symbol: str
    current_r_multiple: Optional[float]
    unrealized_pl_pct: float
    primary_action: ProfitAction
    actions: List[ProfitActionItem]
    warnings: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class HealthData(BaseModel):
    status: str = "healthy"
    service: str = "profit-agent"


class StandardAgentResponse(BaseModel, Generic[T]):
    status: str
    agent_type: str = "profit-agent"
    version: str = "0.1.0"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    data: Optional[T] = None
    error: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
