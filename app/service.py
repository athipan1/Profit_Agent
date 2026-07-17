from __future__ import annotations

from typing import List, Optional

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


def _break_even_stop(request: ProfitPlanRequest, current_r: Optional[float]) -> Optional[float]:
    position = request.position
    if current_r is None or current_r < request.break_even_trigger_r:
        return None
    existing_stop = position.stop_loss or 0
    return max(existing_stop, position.entry_price)


def _trailing_stop(request: ProfitPlanRequest, current_r: Optional[float]) -> Optional[float]:
    position = request.position
    if current_r is None or current_r < request.break_even_trigger_r:
        return None
    if not position.highest_price_since_entry:
        return None
    trailing_stop = position.highest_price_since_entry * (1 - request.trailing_stop_pct)
    if position.stop_loss is not None:
        trailing_stop = max(trailing_stop, position.stop_loss)
    if trailing_stop >= position.current_price:
        return None
    return trailing_stop


def build_profit_plan(request: ProfitPlanRequest) -> ProfitPlanData:
    position = request.position
    current_r = _r_multiple(request)
    actions: List[ProfitActionItem] = []
    warnings: List[str] = []

    if position.highest_price_since_entry_inferred:
        warnings.append(HIGHEST_PRICE_FALLBACK_WARNING)

    if current_r is None:
        warnings.append("risk_per_share is missing; R-multiple based take-profit rules are limited")

    if request.exit_on_stop_breach and position.stop_loss is not None and position.current_price <= position.stop_loss:
        actions.append(
            ProfitActionItem(
                action=ProfitAction.EXIT_ALL,
                symbol=position.symbol.upper(),
                quantity=position.quantity,
                recommended_stop=position.stop_loss,
                reason="Current price is at or below stop loss",
                confidence_score=0.90,
            )
        )
        return ProfitPlanData(
            symbol=position.symbol.upper(),
            current_r_multiple=None if current_r is None else round(current_r, 4),
            unrealized_pl_pct=round(position.unrealized_pl_pct or 0.0, 6),
            primary_action=ProfitAction.EXIT_ALL,
            actions=actions,
            warnings=warnings,
            metadata={"advisory_only": True},
        )

    break_even_stop = _break_even_stop(request, current_r)
    trailing_stop = _trailing_stop(request, current_r)
    recommended_stop = None
    if break_even_stop is not None or trailing_stop is not None:
        recommended_stop = max(break_even_stop or 0, trailing_stop or 0)
        if position.stop_loss is None or recommended_stop > position.stop_loss:
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
                        "trailing_stop": _round_price(trailing_stop),
                    },
                )
            )

    if current_r is not None and current_r >= request.second_take_profit_r:
        actions.append(
            ProfitActionItem(
                action=ProfitAction.PARTIAL_EXIT,
                symbol=position.symbol.upper(),
                quantity=round(position.quantity * request.partial_exit_pct, 6),
                recommended_stop=_round_price(recommended_stop),
                reason=f"Position reached second take-profit target at {request.second_take_profit_r}R",
                confidence_score=0.82,
            )
        )
    elif current_r is not None and current_r >= request.first_take_profit_r:
        actions.append(
            ProfitActionItem(
                action=ProfitAction.PARTIAL_EXIT,
                symbol=position.symbol.upper(),
                quantity=round(position.quantity * request.partial_exit_pct, 6),
                recommended_stop=_round_price(recommended_stop),
                reason=f"Position reached first take-profit target at {request.first_take_profit_r}R",
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

    primary_action = next((item.action for item in actions if item.action != ProfitAction.MOVE_STOP), actions[0].action)
    return ProfitPlanData(
        symbol=position.symbol.upper(),
        current_r_multiple=None if current_r is None else round(current_r, 4),
        unrealized_pl_pct=round(position.unrealized_pl_pct or 0.0, 6),
        primary_action=primary_action,
        actions=actions,
        warnings=warnings,
        metadata={
            "advisory_only": True,
            "first_take_profit_r": request.first_take_profit_r,
            "second_take_profit_r": request.second_take_profit_r,
            "partial_exit_pct": request.partial_exit_pct,
            "trailing_stop_pct": request.trailing_stop_pct,
        },
    )
