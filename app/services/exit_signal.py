from __future__ import annotations

from typing import Literal

from app.models import (
    ProfitAction,
    ProfitExitSignalData,
    ProfitPlanRequest,
)
from app.service import build_profit_plan


EXIT_ACTIONS = {ProfitAction.PARTIAL_EXIT, ProfitAction.EXIT_ALL}
IMMEDIATE_TRIGGERS = {"hard_stop_loss_breach", "trailing_stop_breach"}


def build_exit_signal(request: ProfitPlanRequest) -> ProfitExitSignalData:
    """Project the shared assessment into a compact Risk-gate contract."""
    assessment = build_profit_plan(request)
    should_exit = assessment.primary_action in EXIT_ACTIONS
    selected_action = next(
        (
            item
            for item in assessment.actions
            if item.action == assessment.primary_action
        ),
        None,
    )
    urgency: Literal["immediate", "normal", "none"] = (
        "immediate"
        if should_exit and assessment.trigger in IMMEDIATE_TRIGGERS
        else "normal"
        if should_exit
        else "none"
    )
    return ProfitExitSignalData(
        symbol=assessment.symbol,
        should_exit=should_exit,
        exit_type=assessment.trigger if should_exit else None,
        urgency=urgency,
        recommended_quantity=(selected_action.quantity if selected_action else 0),
        recommended_stop=assessment.recommended_stop,
        requires_risk_approval=should_exit,
        advisory_only=True,
        warnings=assessment.warnings,
        primary_action=assessment.primary_action,
        trigger=assessment.trigger,
        decision_id=assessment.decision_id,
        decision_type=assessment.decision_type,
        position_version=assessment.position_version,
        next_lifecycle_state=assessment.next_lifecycle_state,
        decision_status=assessment.decision_status,
        data_quality=assessment.data_quality,
        policy_source=assessment.policy_source,
        base_trailing_stop_pct=assessment.base_trailing_stop_pct,
        adjusted_trailing_stop_pct=assessment.adjusted_trailing_stop_pct,
        adjustment_reasons=assessment.adjustment_reasons,
        market_constraints=assessment.market_constraints,
    )