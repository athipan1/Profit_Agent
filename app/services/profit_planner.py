from __future__ import annotations

from decimal import Decimal
from typing import Optional

from app.models import (
    PartialExitPolicyData,
    ProfitInitialPlanData,
    ProfitPlanRequest,
    TrailingPolicyData,
)
from app.service import build_profit_plan, round_price_to_market


def _target_price(request: ProfitPlanRequest, target_r: float) -> Optional[float]:
    risk_per_share = request.position.risk_per_share
    if risk_per_share is None:
        return None
    value = Decimal(str(request.position.entry_price)) + (
        Decimal(str(risk_per_share)) * Decimal(str(target_r))
    )
    return round_price_to_market(request, value)


def build_initial_profit_plan(request: ProfitPlanRequest) -> ProfitInitialPlanData:
    """Describe the initial exit policy without duplicating decision logic."""
    assessment = build_profit_plan(request)
    return ProfitInitialPlanData(
        **assessment.model_dump(),
        initial_stop=round_price_to_market(request, request.position.stop_loss),
        first_target_price=_target_price(
            request, assessment.adjusted_first_take_profit_r
        ),
        second_target_price=_target_price(
            request, assessment.adjusted_second_take_profit_r
        ),
        trailing_policy=TrailingPolicyData(
            activation_r=request.break_even_trigger_r,
            trailing_stop_pct=assessment.adjusted_trailing_stop_pct,
        ),
        partial_exit_policy=PartialExitPolicyData(
            first_target_r=assessment.adjusted_first_take_profit_r,
            second_target_r=assessment.adjusted_second_take_profit_r,
            partial_exit_pct=assessment.adjusted_partial_exit_pct,
        ),
    )
