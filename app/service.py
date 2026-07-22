from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.config import market_data_max_age_seconds
from app.models import ProfitAction, ProfitActionItem, ProfitPlanData, ProfitPlanRequest
from app.services.adaptive_policy import (
    AdaptiveProfitPolicy,
    evaluate_adaptive_policy,
    static_policy,
)


HIGHEST_PRICE_FALLBACK_WARNING = (
    "highest_price_since_entry was not provided; trailing stop uses "
    "max(entry_price, current_price) as a fallback and may be understated "
    "because Profit_Agent does not track price history"
)


def _decimal(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _floor_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    units = (value / increment).to_integral_value(rounding=ROUND_DOWN)
    return units * increment


def round_price_to_market(
    request: ProfitPlanRequest,
    value: Optional[Decimal | float],
) -> Optional[float]:
    if value is None:
        return None
    rounded = _floor_to_increment(
        _decimal(value), request.market_constraints.price_increment
    )
    return float(rounded)


def _round_quantity(
    request: ProfitPlanRequest,
    value: Decimal | float,
) -> Optional[float]:
    constraints = request.market_constraints
    remaining = _decimal(
        request.lifecycle.remaining_quantity
        if request.lifecycle is not None
        else request.position.quantity
    )
    rounded = min(
        remaining,
        _floor_to_increment(_decimal(value), constraints.quantity_increment),
    )
    if rounded < constraints.minimum_order_quantity:
        return None
    return float(rounded)


def _exit_quantity(request: ProfitPlanRequest) -> float:
    quantity = _round_quantity(request, _decimal(request.position.quantity))
    if quantity is None:
        raise ValueError("remaining quantity is below market minimum")
    return quantity


def _r_output(value: Optional[Decimal]) -> Optional[float]:
    return None if value is None else float(value.quantize(Decimal("0.0001")))


def _unrealized_pl_pct(request: ProfitPlanRequest) -> float:
    position = request.position
    value = (
        _decimal(position.current_price) - _decimal(position.entry_price)
    ) / _decimal(position.entry_price)
    return float(value.quantize(Decimal("0.000001")))


def _r_multiple(request: ProfitPlanRequest) -> Optional[Decimal]:
    position = request.position
    if not position.risk_per_share or _decimal(position.risk_per_share) <= 0:
        return None
    return (
        _decimal(position.current_price) - _decimal(position.entry_price)
    ) / _decimal(position.risk_per_share)


def _break_even_stop(
    request: ProfitPlanRequest,
    current_r: Optional[Decimal],
) -> Optional[Decimal]:
    position = request.position
    if current_r is None or current_r < _decimal(request.break_even_trigger_r):
        return None
    existing_stop = _decimal(position.stop_loss or 0)
    return max(existing_stop, _decimal(position.entry_price))


def _raw_trailing_stop_decimal(
    request: ProfitPlanRequest,
    current_r: Optional[Decimal | float],
) -> Optional[Decimal]:
    """Calculate the active trailing threshold without hiding a breach."""
    position = request.position
    if current_r is None or _decimal(current_r) < _decimal(
        request.break_even_trigger_r
    ):
        return None
    if not position.highest_price_since_entry:
        return None
    return _decimal(position.highest_price_since_entry) * (
        Decimal("1") - _decimal(request.trailing_stop_pct)
    )


def calculate_raw_trailing_stop(
    request: ProfitPlanRequest,
    current_r: Optional[Decimal | float],
) -> Optional[float]:
    """Compatibility projection; all underlying arithmetic is Decimal."""
    value = _raw_trailing_stop_decimal(request, current_r)
    return None if value is None else float(value)


def detect_trailing_stop_breach(
    request: ProfitPlanRequest,
    raw_trailing_stop: Optional[Decimal | float],
) -> bool:
    """Treat equality as a breach, matching hard stop-loss semantics."""
    return raw_trailing_stop is not None and _decimal(
        request.position.current_price
    ) <= _decimal(raw_trailing_stop)


def calculate_recommended_stop(
    request: ProfitPlanRequest,
    *,
    break_even_stop: Optional[Decimal],
    raw_trailing_stop: Optional[Decimal],
) -> Optional[Decimal]:
    candidates = [
        value
        for value in (
            _decimal(request.position.stop_loss)
            if request.position.stop_loss is not None
            else None,
            break_even_stop,
            raw_trailing_stop,
        )
        if value is not None
    ]
    return max(candidates) if candidates else None


def _unique_warnings(values: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(value for value in values if value))


def _base_warnings(request: ProfitPlanRequest) -> List[str]:
    warnings: List[str] = list(request.warnings)
    if request.position.highest_price_since_entry_inferred:
        warnings.append(HIGHEST_PRICE_FALLBACK_WARNING)
    if request.position.risk_per_share_warning:
        warnings.append(request.position.risk_per_share_warning)
    return _unique_warnings(warnings)


def _data_quality(request: ProfitPlanRequest) -> Dict[str, bool]:
    explicit = request.data_quality
    context = request.market_context
    if explicit is not None:
        return explicit.model_dump()

    market_price_fresh = context is None
    if context is not None and context.observed_at is not None:
        age_seconds = (
            datetime.now(timezone.utc) - context.observed_at.astimezone(timezone.utc)
        ).total_seconds()
        market_price_fresh = 0 <= age_seconds <= market_data_max_age_seconds()
    return {
        "market_price_fresh": market_price_fresh,
        "peak_history_complete": not request.position.highest_price_since_entry_inferred,
        "position_version_current": True,
        "emergency_halt_active": False,
    }


def _quality_block_reason(
    request: ProfitPlanRequest,
    quality: Dict[str, bool],
) -> Optional[str]:
    if not quality["market_price_fresh"] and (
        request.market_context is not None or request.data_quality is not None
    ):
        return "market data is stale or has no freshness evidence"
    if request.data_quality is not None and not quality["peak_history_complete"]:
        return "peak price history is incomplete"
    if request.data_quality is not None and not quality["position_version_current"]:
        return "position version is stale"
    return None


def _review_plan(
    request: ProfitPlanRequest,
    *,
    current_r: Optional[Decimal],
    warnings: List[str],
    quality: Dict[str, bool],
    reason: str,
    trigger: str,
    policy: AdaptiveProfitPolicy,
) -> ProfitPlanData:
    position = request.position
    review_warnings = _unique_warnings([*warnings, reason])
    return ProfitPlanData(
        symbol=position.symbol.upper(),
        current_r_multiple=_r_output(current_r),
        unrealized_pl_pct=_unrealized_pl_pct(request),
        primary_action=ProfitAction.REVIEW,
        actions=[
            ProfitActionItem(
                action=ProfitAction.REVIEW,
                symbol=position.symbol.upper(),
                quantity=0,
                reason=reason,
                confidence_score=1.0,
                metadata={"trigger": trigger, "advisory_only": True},
            )
        ],
        warnings=review_warnings,
        trigger=trigger,
        requires_risk_approval=False,
        advisory_only=True,
        decision_status="blocked",
        data_quality=quality,
        market_constraints=request.market_constraints,
        metadata={
            "advisory_only": True,
            "requires_risk_approval": False,
            "request_metadata": request.metadata,
        },
        **policy.response_fields(request),
    )


def _decision_fields(
    request: ProfitPlanRequest,
    *,
    decision_type: Optional[str],
    suffix: Optional[str],
    next_lifecycle_state: Optional[Dict[str, bool]] = None,
) -> Dict[str, Any]:
    """Build a deterministic identity without owning or mutating lifecycle state."""
    lifecycle = request.lifecycle
    if lifecycle is None or decision_type is None or suffix is None:
        return {}
    return {
        "decision_id": (
            f"profit:{lifecycle.position_id}:{request.position.symbol.upper()}:"
            f"v{lifecycle.position_version}:{suffix}"
        ),
        "decision_type": decision_type,
        "position_version": lifecycle.position_version,
        "next_lifecycle_state": next_lifecycle_state or {},
    }


def _pending_profit_target(
    request: ProfitPlanRequest,
    current_r: Optional[Decimal],
) -> Tuple[Optional[str], Optional[str]]:
    """Return only the next unexecuted target for a lifecycle-aware request."""
    if current_r is None:
        return None, None
    lifecycle = request.lifecycle
    if lifecycle is None:
        if current_r >= _decimal(request.second_take_profit_r):
            return "second_take_profit", "second_take_profit"
        if current_r >= _decimal(request.first_take_profit_r):
            return "first_take_profit", "first_take_profit"
        return None, None

    # Targets are intentionally sequential. Crossing both targets initially
    # proposes TP1 first; TP2 can only be proposed after TP1 is confirmed by DB.
    if (
        current_r >= _decimal(request.first_take_profit_r)
        and not lifecycle.first_target_executed
    ):
        return "first_take_profit", "first_take_profit"
    if (
        current_r >= _decimal(request.second_take_profit_r)
        and lifecycle.first_target_executed
        and not lifecycle.second_target_executed
    ):
        return "second_take_profit", "second_take_profit"
    return None, None


def build_profit_plan(request: ProfitPlanRequest) -> ProfitPlanData:
    position = request.position
    actions: List[ProfitActionItem] = []
    warnings = _base_warnings(request)
    quality = _data_quality(request)
    base_policy = static_policy(request)

    if quality["emergency_halt_active"]:
        return _review_plan(
            request,
            current_r=None,
            warnings=warnings,
            quality=quality,
            reason="emergency halt is active",
            trigger="emergency_halt",
            policy=base_policy,
        )

    # Hard stop-loss safety has priority over every profit calculation.
    if position.stop_loss is not None and _decimal(position.current_price) <= _decimal(
        position.stop_loss
    ):
        actions.append(
            ProfitActionItem(
                action=ProfitAction.EXIT_ALL,
                symbol=position.symbol.upper(),
                quantity=_exit_quantity(request),
                recommended_stop=round_price_to_market(request, position.stop_loss),
                reason="Current price is at or below stop loss",
                confidence_score=0.90,
                metadata={
                    "trigger": "hard_stop_loss_breach",
                    "advisory_only": True,
                    "requires_risk_approval": True,
                },
            )
        )
        return ProfitPlanData(
            symbol=position.symbol.upper(),
            current_r_multiple=None,
            unrealized_pl_pct=_unrealized_pl_pct(request),
            primary_action=ProfitAction.EXIT_ALL,
            actions=actions,
            warnings=warnings,
            trigger="hard_stop_loss_breach",
            recommended_stop=round_price_to_market(request, position.stop_loss),
            requires_risk_approval=True,
            advisory_only=True,
            metadata={
                "advisory_only": True,
                "requires_risk_approval": True,
                "request_metadata": request.metadata,
            },
            data_quality=quality,
            market_constraints=request.market_constraints,
            **base_policy.response_fields(request),
            **_decision_fields(
                request,
                decision_type="hard_stop_exit",
                suffix="hard-stop",
            ),
        )

    current_r = _r_multiple(request)
    if current_r is None:
        warnings = _unique_warnings(
            [
                *warnings,
                "risk_per_share is missing; R-multiple based take-profit rules are limited",
            ]
        )

    # Keep the raw threshold even when price has crossed it; a breach is an exit signal.
    raw_trailing_stop = _raw_trailing_stop_decimal(request, current_r)
    if detect_trailing_stop_breach(request, raw_trailing_stop):
        recommended_stop = round_price_to_market(request, raw_trailing_stop)
        actions.append(
            ProfitActionItem(
                action=ProfitAction.EXIT_ALL,
                symbol=position.symbol.upper(),
                quantity=_exit_quantity(request),
                recommended_stop=recommended_stop,
                reason="Current price is at or below the active trailing stop",
                confidence_score=0.92,
                metadata={
                    "trigger": "trailing_stop_breach",
                    "advisory_only": True,
                    "requires_risk_approval": True,
                },
            )
        )
        return ProfitPlanData(
            symbol=position.symbol.upper(),
            current_r_multiple=_r_output(current_r),
            unrealized_pl_pct=_unrealized_pl_pct(request),
            primary_action=ProfitAction.EXIT_ALL,
            actions=actions,
            warnings=warnings,
            trigger="trailing_stop_breach",
            recommended_stop=recommended_stop,
            requires_risk_approval=True,
            advisory_only=True,
            metadata={
                "advisory_only": True,
                "requires_risk_approval": True,
                "raw_trailing_stop": recommended_stop,
                "request_metadata": request.metadata,
            },
            data_quality=quality,
            market_constraints=request.market_constraints,
            **base_policy.response_fields(request),
            **_decision_fields(
                request,
                decision_type="trailing_stop_exit",
                suffix="trailing-stop",
            ),
        )

    quality_reason = _quality_block_reason(request, quality)
    if quality_reason is not None:
        return _review_plan(
            request,
            current_r=current_r,
            warnings=warnings,
            quality=quality,
            reason=quality_reason,
            trigger="data_quality_block",
            policy=base_policy,
        )

    policy = evaluate_adaptive_policy(request)
    policy_fields = policy.response_fields(request)
    policy_request = request
    effective_request = request.model_copy(
        update={
            "first_take_profit_r": policy.first_take_profit_r,
            "second_take_profit_r": policy.second_take_profit_r,
            "partial_exit_pct": policy.partial_exit_pct,
            "trailing_stop_pct": policy.trailing_stop_pct,
        }
    )
    adjusted_trailing_stop = _raw_trailing_stop_decimal(effective_request, current_r)
    if detect_trailing_stop_breach(effective_request, adjusted_trailing_stop):
        recommended_stop = round_price_to_market(request, adjusted_trailing_stop)
        actions.append(
            ProfitActionItem(
                action=ProfitAction.EXIT_ALL,
                symbol=position.symbol.upper(),
                quantity=_exit_quantity(request),
                recommended_stop=recommended_stop,
                reason="Current price is at or below the adaptive trailing stop",
                confidence_score=0.92,
                metadata={
                    "trigger": "trailing_stop_breach",
                    "advisory_only": True,
                    "requires_risk_approval": True,
                    "policy_source": policy.source,
                },
            )
        )
        return ProfitPlanData(
            symbol=position.symbol.upper(),
            current_r_multiple=_r_output(current_r),
            unrealized_pl_pct=_unrealized_pl_pct(request),
            primary_action=ProfitAction.EXIT_ALL,
            actions=actions,
            warnings=warnings,
            trigger="trailing_stop_breach",
            recommended_stop=recommended_stop,
            requires_risk_approval=True,
            advisory_only=True,
            data_quality=quality,
            market_constraints=request.market_constraints,
            metadata={
                "advisory_only": True,
                "requires_risk_approval": True,
                "raw_trailing_stop": recommended_stop,
                "request_metadata": request.metadata,
            },
            **policy.response_fields(request),
            **_decision_fields(
                request,
                decision_type="trailing_stop_exit",
                suffix="trailing-stop",
            ),
        )

    request = effective_request
    raw_trailing_stop = adjusted_trailing_stop

    break_even_stop = _break_even_stop(request, current_r)
    recommended_stop = calculate_recommended_stop(
        request,
        break_even_stop=break_even_stop,
        raw_trailing_stop=raw_trailing_stop,
    )
    if break_even_stop is not None or raw_trailing_stop is not None:
        if position.stop_loss is None or (
            recommended_stop is not None
            and recommended_stop > _decimal(position.stop_loss)
        ):
            actions.append(
                ProfitActionItem(
                    action=ProfitAction.MOVE_STOP,
                    symbol=position.symbol.upper(),
                    quantity=0,
                    recommended_stop=round_price_to_market(request, recommended_stop),
                    reason="Move stop to lock profit or reduce downside risk",
                    confidence_score=0.72,
                    metadata={
                        "break_even_stop": round_price_to_market(
                            request, break_even_stop
                        ),
                        "trailing_stop": round_price_to_market(
                            request, raw_trailing_stop
                        ),
                    },
                )
            )

    trigger, decision_type = _pending_profit_target(request, current_r)
    partial_quantity = (
        _round_quantity(
            request,
            _decimal(position.quantity) * _decimal(request.partial_exit_pct),
        )
        if trigger in {"first_take_profit", "second_take_profit"}
        else None
    )
    if trigger is not None and partial_quantity is None:
        return _review_plan(
            policy_request,
            current_r=current_r,
            warnings=warnings,
            quality=quality,
            reason=(
                "calculated partial exit is below minimum_order_quantity after "
                "quantity-increment rounding"
            ),
            trigger="partial_exit_below_market_minimum",
            policy=policy,
        )
    if trigger == "second_take_profit":
        actions.append(
            ProfitActionItem(
                action=ProfitAction.PARTIAL_EXIT,
                symbol=position.symbol.upper(),
                quantity=partial_quantity,
                recommended_stop=round_price_to_market(request, recommended_stop),
                reason=(
                    "Position reached second take-profit target at "
                    f"{request.second_take_profit_r}R"
                ),
                confidence_score=0.82,
            )
        )
    elif trigger == "first_take_profit":
        actions.append(
            ProfitActionItem(
                action=ProfitAction.PARTIAL_EXIT,
                symbol=position.symbol.upper(),
                quantity=partial_quantity,
                recommended_stop=round_price_to_market(request, recommended_stop),
                reason=(
                    "Position reached first take-profit target at "
                    f"{request.first_take_profit_r}R"
                ),
                confidence_score=0.78,
            )
        )

    if not actions:
        actions.append(
            ProfitActionItem(
                action=ProfitAction.HOLD,
                symbol=position.symbol.upper(),
                quantity=0,
                recommended_stop=round_price_to_market(request, recommended_stop),
                reason="No take-profit or exit condition is triggered",
                confidence_score=0.65,
            )
        )

    primary_action = next(
        (item.action for item in actions if item.action != ProfitAction.MOVE_STOP),
        actions[0].action,
    )
    requires_risk_approval = primary_action != ProfitAction.HOLD
    if decision_type == "first_take_profit":
        decision_fields = _decision_fields(
            request,
            decision_type=decision_type,
            suffix="tp1",
            next_lifecycle_state={"first_target_executed": True},
        )
    elif decision_type == "second_take_profit":
        decision_fields = _decision_fields(
            request,
            decision_type=decision_type,
            suffix="tp2",
            next_lifecycle_state={"second_target_executed": True},
        )
    elif primary_action == ProfitAction.MOVE_STOP:
        decision_fields = _decision_fields(
            request,
            decision_type="stop_adjustment",
            suffix="stop-adjustment",
        )
    else:
        decision_fields = {}
    return ProfitPlanData(
        symbol=position.symbol.upper(),
        current_r_multiple=_r_output(current_r),
        unrealized_pl_pct=_unrealized_pl_pct(request),
        primary_action=primary_action,
        actions=actions,
        warnings=warnings,
        trigger=trigger,
        recommended_stop=round_price_to_market(request, recommended_stop),
        requires_risk_approval=requires_risk_approval,
        advisory_only=True,
        metadata={
            "advisory_only": True,
            "requires_risk_approval": requires_risk_approval,
            "first_take_profit_r": request.first_take_profit_r,
            "second_take_profit_r": request.second_take_profit_r,
            "partial_exit_pct": request.partial_exit_pct,
            "trailing_stop_pct": request.trailing_stop_pct,
            "raw_trailing_stop": round_price_to_market(request, raw_trailing_stop),
            "request_metadata": request.metadata,
        },
        data_quality=quality,
        market_constraints=request.market_constraints,
        **policy_fields,
        **decision_fields,
    )
