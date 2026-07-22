import pytest
from pydantic import ValidationError

from app.models import ProfitAction, ProfitPlanRequest
from app.service import build_profit_plan


def _payload(*, current_price=112, lifecycle=None):
    body = {
        "position": {
            "symbol": "ACGL",
            "quantity": 10,
            "entry_price": 100,
            "current_price": current_price,
            "stop_loss": 96,
            "highest_price_since_entry": max(100, current_price),
        },
        "first_take_profit_r": 2,
        "second_take_profit_r": 3,
        "partial_exit_pct": 0.3,
        "trailing_stop_pct": 0.08,
        "break_even_trigger_r": 1,
    }
    if lifecycle is not None:
        body["lifecycle"] = {
            "position_id": "account-1:position-42",
            "position_version": 7,
            "first_target_executed": False,
            "second_target_executed": False,
            "total_exited_quantity": 0,
            "remaining_quantity": 10,
            **lifecycle,
        }
    return body


def test_first_target_decision_id_is_deterministic():
    request = ProfitPlanRequest.model_validate(
        _payload(current_price=108, lifecycle={})
    )

    first = build_profit_plan(request)
    second = build_profit_plan(request)

    assert first.primary_action == ProfitAction.PARTIAL_EXIT
    assert first.decision_type == "first_take_profit"
    assert first.decision_id == ("profit:account-1:position-42:ACGL:v7:tp1")
    assert second.decision_id == first.decision_id
    assert first.position_version == 7
    assert first.next_lifecycle_state == {"first_target_executed": True}


def test_crossing_both_targets_proposes_first_target_first():
    result = build_profit_plan(
        ProfitPlanRequest.model_validate(_payload(current_price=112, lifecycle={}))
    )

    assert result.decision_type == "first_take_profit"
    assert result.trigger == "first_take_profit"
    assert result.decision_id.endswith(":tp1")


def test_first_target_is_not_recommended_twice():
    result = build_profit_plan(
        ProfitPlanRequest.model_validate(
            _payload(
                current_price=109,
                lifecycle={"first_target_executed": True, "position_version": 8},
            )
        )
    )

    assert all(action.action != ProfitAction.PARTIAL_EXIT for action in result.actions)
    assert result.decision_type != "first_take_profit"
    assert result.trigger is None


def test_second_target_is_proposed_after_first_fill():
    result = build_profit_plan(
        ProfitPlanRequest.model_validate(
            _payload(
                current_price=112,
                lifecycle={
                    "first_target_executed": True,
                    "position_version": 8,
                    "total_exited_quantity": 3,
                },
            )
        )
    )

    assert result.primary_action == ProfitAction.PARTIAL_EXIT
    assert result.decision_type == "second_take_profit"
    assert result.decision_id.endswith(":v8:tp2")
    assert result.next_lifecycle_state == {"second_target_executed": True}


def test_second_target_is_not_recommended_twice():
    result = build_profit_plan(
        ProfitPlanRequest.model_validate(
            _payload(
                current_price=112,
                lifecycle={
                    "first_target_executed": True,
                    "second_target_executed": True,
                    "position_version": 9,
                    "total_exited_quantity": 6,
                },
            )
        )
    )

    assert all(action.action != ProfitAction.PARTIAL_EXIT for action in result.actions)
    assert result.decision_type != "second_take_profit"
    assert result.trigger is None


@pytest.mark.parametrize(
    ("lifecycle", "message"),
    [
        (
            {"first_target_executed": False, "second_target_executed": True},
            "requires first_target_executed",
        ),
        ({"remaining_quantity": 9}, "must match position.quantity"),
        ({"position_version": 0}, "greater than or equal to 1"),
        ({"position_id": "bad position id"}, "invalid format"),
    ],
)
def test_malformed_lifecycle_is_rejected(lifecycle, message):
    with pytest.raises(ValidationError, match=message):
        ProfitPlanRequest.model_validate(_payload(lifecycle=lifecycle))


def test_exit_decision_uses_same_position_version():
    result = build_profit_plan(
        ProfitPlanRequest.model_validate(
            _payload(current_price=96, lifecycle={"position_version": 11})
        )
    )

    assert result.primary_action == ProfitAction.EXIT_ALL
    assert result.decision_type == "hard_stop_exit"
    assert result.decision_id.endswith(":v11:hard-stop")
    assert result.position_version == 11


def test_legacy_request_remains_accepted_during_migration():
    result = build_profit_plan(ProfitPlanRequest.model_validate(_payload()))

    assert result.primary_action == ProfitAction.PARTIAL_EXIT
    assert result.decision_id is None
    assert result.position_version is None