from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.models import MarketRegime, MarketRiskLevel, ProfitPlanRequest


@dataclass(frozen=True)
class AdaptiveProfitPolicy:
    first_take_profit_r: Decimal
    second_take_profit_r: Decimal
    partial_exit_pct: Decimal
    trailing_stop_pct: Decimal
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


def _decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _bounded(value: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
    return min(upper, max(lower, value))


def static_policy(request: ProfitPlanRequest) -> AdaptiveProfitPolicy:
    return AdaptiveProfitPolicy(
        first_take_profit_r=_decimal(request.first_take_profit_r),
        second_take_profit_r=_decimal(request.second_take_profit_r),
        partial_exit_pct=_decimal(request.partial_exit_pct),
        trailing_stop_pct=_decimal(request.trailing_stop_pct),
        source="static_v1",
        reasons=(),
    )


def evaluate_adaptive_policy(request: ProfitPlanRequest) -> AdaptiveProfitPolicy:
    context = request.market_context
    if context is None:
        return static_policy(request)

    first_target = _decimal(request.first_take_profit_r)
    second_target = _decimal(request.second_take_profit_r)
    partial_exit = _decimal(request.partial_exit_pct)
    trailing_stop = _decimal(request.trailing_stop_pct)
    reasons: list[str] = []

    if context.regime == MarketRegime.BULL and _decimal(
        context.trend_strength or 0
    ) >= Decimal("0.70"):
        trailing_stop *= Decimal("1.25")
        partial_exit *= Decimal("0.75")
        reasons.append("bull regime with strong trend")

    high_volatility = (
        context.regime == MarketRegime.VOLATILE
        or _decimal(context.volatility_percentile or 0) >= Decimal("75")
        or _decimal(context.atr_pct or 0) >= Decimal("0.04")
    )
    if high_volatility:
        if context.atr_pct is not None:
            trailing_stop = max(
                trailing_stop,
                _bounded(
                    _decimal(context.atr_pct) * Decimal("2.5"),
                    Decimal("0.03"),
                    Decimal("0.20"),
                ),
            )
        partial_exit *= Decimal("1.25")
        reasons.append("high volatility")

    if context.regime == MarketRegime.BEAR and _decimal(
        context.trend_strength or 0
    ) < Decimal("0.40"):
        first_target *= Decimal("0.80")
        second_target *= Decimal("0.80")
        trailing_stop *= Decimal("0.75")
        partial_exit *= Decimal("1.25")
        reasons.append("bear regime with weak trend")

    if context.risk_level == MarketRiskLevel.HIGH:
        trailing_stop *= Decimal("0.85")
        partial_exit *= Decimal("1.15")
        reasons.append("high market risk")

    if context.upcoming_event_risk:
        trailing_stop *= Decimal("0.75")
        partial_exit *= Decimal("1.25")
        reasons.append("upcoming event risk")

    quantum = Decimal("0.000001")
    first_target = max(Decimal("0.01"), first_target)
    second_target = max(first_target + Decimal("0.01"), second_target)
    return AdaptiveProfitPolicy(
        first_take_profit_r=first_target.quantize(quantum),
        second_take_profit_r=second_target.quantize(quantum),
        partial_exit_pct=_bounded(
            partial_exit, Decimal("0.000001"), Decimal("1")
        ).quantize(quantum),
        trailing_stop_pct=_bounded(
            trailing_stop, Decimal("0.000001"), Decimal("1")
        ).quantize(quantum),
        source="deterministic_adaptive_v1",
        reasons=tuple(reasons),
    )
