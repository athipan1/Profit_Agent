from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.models import ProfitAction, ProfitActionItem, ProfitPlanData, ProfitPlanRequest


HIGHEST_PRICE_FALLBACK_WARNING = (
    "highest_price_since_entry was not provided; trailing stop uses "
    "max(entry_price, current_price) as a fallback and may be understated "
    "because Profit_Agent does not track price history"
)


def _round_price(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(value, 2)


def _r_multiple(request: ProfitPlanRequest) -> Optional[float]:
    position = request.position
    if not position.risk_per_share or position.risk_per_share <= 0:
        return None
    return (position.current_price - position.entry_price) / position.risk_per_share


def _break_even_stop(
    request: ProfitPlanRequest,
    current_r: Optional[float],
) -> Optional[float]:
    position = request.position
    if current_r is None or current_r < request.break_even_trigger_r:
        return None
    existing_stop = position.stop_loss or 0
    return max(existing_stop, position.entry_price)


def calculate_raw_trailing_stop(
    request: ProfitPlanRequest,
    current_r: Optional[float],
) -> Optional[float]:
    """Calculate the active trailing threshold without hiding a breach."""
    position = request.position
    if current_r is None or current_r < request.break_even_trigger_r:
        return None
    if not position.highest_price_since_entry:
        return None
    return position.highest_price_since_entry * (1 - request.trailing_stop_pct)


def detect_trailing_stop_breach(
    request: ProfitPlanRequest,
    raw_trailing_stop: Optional[float],
) -> bool:
    """Treat equality as a breach, matching hard stop-loss semantics."""
    return (
        raw_trailing_stop is not None
        and request.position.current_price <= raw_trailing_stop
    )


def calculate_recommended_stop(
    request: ProfitPlanRequest,
    *,
    break_even_stop: Optional[float],
    raw_trailing_stop: Optional[float],
) -> Optional[float]:
    candidates = [
        value
        for value in (
            request.position.stop_loss,
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
    current_r: Optional[float],
) -> Tuple[Optional[str], Optional[str]]:
    """Return only the next unexecuted target for a lifecycle-aware request."""
    if current_r is None:
        return None, None
    lifecycle = request.lifecycle
    if lifecycle is None:
        if current_r >= request.second_take_profit_r:
            return "second_take_profit", "second_take_profit"
        if current_r >= request.first_take_profit_r:
            return "first_take_profit", "first_take_profit"
        return None, None

    # Targets are intentionally sequential. Crossing both targets initially
    # proposes TP1 first; TP2 can only be proposed after TP1 is confirmed by DB.
    if (
        current_r >= request.first_take_profit_r
        and not lifecycle.first_target_executed
    ):
        return "first_take_profit", "first_take_profit"
    if (
        current_r >= request.second_take_profit_r
        and lifecycle.first_target_executed
        and not lifecycle.second_target_executed
    ):
        return "second_take_profit", "second_take_profit"
    return None, None


def build_profit_plan(request: ProfitPlanRequest) -> ProfitPlanData:
    position = request.position
    actions: List[ProfitActionItem] = []
    warnings = _base_warnings(request)

    # Hard stop-loss safety has priority over every profit calculation.
    if position.stop_loss is not None and position.current_price <= position.stop_loss:
        actions.append(
            ProfitActionItem(
                action=ProfitAction.EXIT_ALL,
                symbol=position.symbol.upper(),
                quantity=position.quantity,
                recommended_stop=position.stop_loss,
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
            unrealized_pl_pct=round(position.unrealized_pl_pct or 0.0, 6),
            primary_action=ProfitAction.EXIT_ALL,
            actions=actions,
            warnings=warnings,
            trigger="hard_stop_loss_breach",
            recommended_stop=_round_price(position.stop_loss),
            requires_risk_approval=True,
            advisory_only=True,
            metadata={
                "advisory_only": True,
                "requires_risk_approval": True,
                "request_metadata": request.metadata,
            },
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
    raw_trailing_stop = calculate_raw_trailing_stop(request, current_r)
    if detect_trailing_stop_breach(request, raw_trailing_stop):
        recommended_stop = _round_price(raw_trailing_stop)
        actions.append(
            ProfitActionItem(
                action=ProfitAction.EXIT_ALL,
                symbol=position.symbol.upper(),
                quantity=position.quantity,
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
            current_r_multiple=None if current_r is None else round(current_r, 4),
            unrealized_pl_pct=round(position.unrealized_pl_pct or 0.0, 6),
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
            **_decision_fields(
                request,
                decision_type="trailing_stop_exit",
                suffix="trailing-stop",
            ),
        )

    break_even_stop = _break_even_stop(request, current_r)
    recommended_stop = calculate_recommended_stop(
        request,
        break_even_stop=break_even_stop,
        raw_trailing_stop=raw_trailing_stop,
    )
    if break_even_stop is not None or raw_trailing_stop is not None:
        if position.stop_loss is None or (
            recommended_stop is not None and recommended_stop > position.stop_loss
        ):
            actions.append(
                ProfitActionItem(
                    action=ProfitAction.MOVE_STOP,
                    symbol=position.symbol.upper(),
                    quantity=0,
                    recommended_stop=_round_price(recommended_stop),
                    reason="Move stop to lock profit or reduce downside risk",
                    confidence_score=0.72,
                    metadata={
                        "break_even_stop": _round_price(break_even_stop),
                        "trailing_stop": _round_price(raw_trailing_stop),
                    },
                )
            )

    trigger, decision_type = _pending_profit_target(request, current_r)
    if trigger == "second_take_profit":
        actions.append(
            ProfitActionItem(
                action=ProfitAction.PARTIAL_EXIT,
                symbol=position.symbol.upper(),
                quantity=round(position.quantity * request.partial_exit_pct, 6),
                recommended_stop=_round_price(recommended_stop),
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
                quantity=round(position.quantity * request.partial_exit_pct, 6),
                recommended_stop=_round_price(recommended_stop),
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
                recommended_stop=_round_price(recommended_stop),
                reason="No take-profit or exit condition is triggered",
                confidence_score=0.65,
            )
        )

    primary_action = next(
        (
            item.action
            for item in actions
            if item.action != ProfitAction.MOVE_STOP
        ),
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
        current_r_multiple=None if current_r is None else round(current_r, 4),
        unrealized_pl_pct=round(position.unrealized_pl_pct or 0.0, 6),
        primary_action=primary_action,
        actions=actions,
        warnings=warnings,
        trigger=trigger,
        recommended_stop=_round_price(recommended_stop),
        requires_risk_approval=requires_risk_approval,
        advisory_only=True,
        metadata={
            "advisory_only": True,
            "requires_risk_approval": requires_risk_approval,
            "first_take_profit_r": request.first_take_profit_r,
            "second_take_profit_r": request.second_take_profit_r,
            "partial_exit_pct": request.partial_exit_pct,
            "trailing_stop_pct": request.trailing_stop_pct,
            "raw_trailing_stop": _round_price(raw_trailing_stop),
            "request_metadata": request.metadata,
        },
        **decision_fields,
    )
