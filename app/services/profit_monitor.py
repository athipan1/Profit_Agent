from __future__ import annotations

from typing import Optional

from app.models import (
    ProfitMonitorData,
    ProfitPlanData,
    ProfitPlanRequest,
    ProfitStage,
    ProfitTargetStatusData,
)
from app.service import build_profit_plan


def _profit_stage(
    request: ProfitPlanRequest,
    assessment: ProfitPlanData,
) -> ProfitStage:
    if assessment.trigger == "hard_stop_loss_breach":
        return ProfitStage.HARD_STOP_BREACH
    if assessment.trigger == "trailing_stop_breach":
        return ProfitStage.TRAILING_STOP_BREACH
    lifecycle = request.lifecycle
    if lifecycle is not None and lifecycle.second_target_executed:
        return ProfitStage.TARGETS_COMPLETE
    current_r = assessment.current_r_multiple
    if current_r is None:
        return ProfitStage.R_UNAVAILABLE
    if current_r >= assessment.adjusted_second_take_profit_r:
        return ProfitStage.SECOND_TARGET_REACHED
    if current_r >= assessment.adjusted_first_take_profit_r:
        return ProfitStage.FIRST_TARGET_REACHED
    if current_r >= request.break_even_trigger_r:
        return ProfitStage.BREAK_EVEN_ACTIVE
    return ProfitStage.BELOW_BREAK_EVEN


def _target_reached(current_r: Optional[float], threshold: float) -> bool:
    return current_r is not None and current_r >= threshold


def build_profit_monitor(request: ProfitPlanRequest) -> ProfitMonitorData:
    """Return current position stage and Database-owned target status."""
    assessment = build_profit_plan(request)
    lifecycle = request.lifecycle
    current_r = assessment.current_r_multiple
    return ProfitMonitorData(
        **assessment.model_dump(),
        current_r=current_r,
        profit_stage=_profit_stage(request, assessment),
        target_status=ProfitTargetStatusData(
            lifecycle_available=lifecycle is not None,
            first_target_reached=_target_reached(
                current_r, assessment.adjusted_first_take_profit_r
            ),
            first_target_executed=(
                lifecycle.first_target_executed if lifecycle is not None else False
            ),
            second_target_reached=_target_reached(
                current_r, assessment.adjusted_second_take_profit_r
            ),
            second_target_executed=(
                lifecycle.second_target_executed if lifecycle is not None else False
            ),
            remaining_quantity=(
                lifecycle.remaining_quantity
                if lifecycle is not None
                else request.position.quantity
            ),
        ),
    )
