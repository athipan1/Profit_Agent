from __future__ import annotations

from dataclasses import dataclass

from app.models import MarketRegime, MarketRiskLevel, ProfitPlanRequest


@dataclass(frozen=True)
class AdaptiveProfitPolicy:
    first_take_profit_r: float
    second_take_profit_r: float
    partial_exit_pct: float
    trailing_stop_pct: float
    source: str
    reasons: tuple[str, ...]

    def response_fields(self, request: ProfitPlanRequest) -> dict[str, object]:
        return {
            "policy_source": self.source,
            "base_trailing_stop_pct": request.trailing_stop_pct,
            "adjusted_trailing_stop_pct": self.trailing_stop_pct,
            "adjusted_first_take_profit_r": self.first_take_profit_r,
            "adjusted_second_take_profit_r": self.second_take_profit_r,
            "adjusted_partial_exit_pct": self.partial_exit_pct,
            "adjustment_reasons": list(self.reasons),
        }


def _bounded(value: float, lower: float, upper: float) -> float:
    return min(upper, max(lower, value))


def static_policy(request: ProfitPlanRequest) -> AdaptiveProfitPolicy:
    return AdaptiveProfitPolicy(
        first_take_profit_r=request.first_take_profit_r,
        second_take_profit_r=request.second_take_profit_r,
        partial_exit_pct=request.partial_exit_pct,
        trailing_stop_pct=request.trailing_stop_pct,
        source="static_v1",
        reasons=(),
    )


def evaluate_adaptive_policy(request: ProfitPlanRequest) -> AdaptiveProfitPolicy:
    context = request.market_context
    if context is None:
        return static_policy(request)

    first_target = request.first_take_profit_r
    second_target = request.second_take_profit_r
    partial_exit = request.partial_exit_pct
    trailing_stop = request.trailing_stop_pct
    reasons: list[str] = []

    if context.regime == MarketRegime.BULL and (context.trend_strength or 0) >= 0.70:
        trailing_stop *= 1.25
        partial_exit *= 0.75
        reasons.append("bull regime with strong trend")

    high_volatility = (
        context.regime == MarketRegime.VOLATILE
        or (context.volatility_percentile or 0) >= 75
        or (context.atr_pct or 0) >= 0.04
    )
    if high_volatility:
        if context.atr_pct is not None:
            trailing_stop = max(
                trailing_stop,
                _bounded(context.atr_pct * 2.5, 0.03, 0.20),
            )
        partial_exit *= 1.25
        reasons.append("high volatility")

    if context.regime == MarketRegime.BEAR and (context.trend_strength or 0) < 0.40:
        first_target *= 0.80
        second_target *= 0.80
        trailing_stop *= 0.75
        partial_exit *= 1.25
        reasons.append("bear regime with weak trend")

    if context.risk_level == MarketRiskLevel.HIGH:
        trailing_stop *= 0.85
        partial_exit *= 1.15
        reasons.append("high market risk")

    if context.upcoming_event_risk:
        trailing_stop *= 0.75
        partial_exit *= 1.25
        reasons.append("upcoming event risk")

    first_target = max(0.01, first_target)
    second_target = max(first_target + 0.01, second_target)
    return AdaptiveProfitPolicy(
        first_take_profit_r=round(first_target, 6),
        second_take_profit_r=round(second_target, 6),
        partial_exit_pct=round(_bounded(partial_exit, 0.000001, 1), 6),
        trailing_stop_pct=round(_bounded(trailing_stop, 0.000001, 1), 6),
        source="deterministic_adaptive_v1",
        reasons=tuple(reasons),
    )
