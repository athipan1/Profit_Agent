from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.models import ProfitAction, ProfitPlanRequest
from app.service import build_profit_plan, calculate_raw_trailing_stop


client = TestClient(app)
AUTH_HEADERS = {"X-API-KEY": "test-profit-api-key"}


def _payload(**overrides):
    payload = {
        "schema_version": "profit-decision.v2",
        "position": {
            "symbol": "ACGL",
            "quantity": 0.333333,
            "entry_price": 100,
            "current_price": 100.2,
            "stop_loss": 99.9,
            "highest_price_since_entry": 100.2,
        },
        "first_take_profit_r": 2,
        "second_take_profit_r": 3,
        "partial_exit_pct": 0.3,
        "trailing_stop_pct": 0.08,
    }
    for key, value in overrides.items():
        if key == "position":
            payload["position"].update(value)
        else:
            payload[key] = value
    return payload


def _decision(payload):
    return build_profit_plan(ProfitPlanRequest.model_validate(payload))


def test_fractional_partial_quantity_is_rounded_down_to_increment():
    result = _decision(_payload())
    partial = next(
        action
        for action in result.actions
        if action.action == ProfitAction.PARTIAL_EXIT
    )

    assert partial.quantity == 0.099999
    assert partial.quantity <= 0.333333
    assert Decimal(str(partial.quantity)) % Decimal("0.000001") == 0


def test_partial_exit_below_market_minimum_returns_blocked_review_not_zero_order():
    result = _decision(
        _payload(
            position={"quantity": 0.03},
            market_constraints={
                "price_increment": "0.01",
                "quantity_increment": "0.01",
                "minimum_order_quantity": "0.01",
            },
        )
    )

    assert result.primary_action == ProfitAction.REVIEW
    assert result.trigger == "partial_exit_below_market_minimum"
    assert result.decision_status == "blocked"
    assert all(action.action != ProfitAction.PARTIAL_EXIT for action in result.actions)


def test_non_power_of_ten_tick_rounds_to_actual_increment():
    result = _decision(
        _payload(
            position={
                "entry_price": 100,
                "current_price": 110,
                "stop_loss": 94,
                "highest_price_since_entry": 120.03,
            },
            market_constraints={
                "price_increment": "0.05",
                "quantity_increment": "0.000001",
                "minimum_order_quantity": "0.000001",
            },
        )
    )

    assert result.trigger == "trailing_stop_breach"
    assert result.recommended_stop == 110.4
    assert Decimal(str(result.recommended_stop)) % Decimal("0.05") == 0


def test_initial_targets_are_rounded_down_to_tick_size():
    response = client.post(
        "/profit/plan",
        headers=AUTH_HEADERS,
        json=_payload(
            position={
                "entry_price": 100.03,
                "current_price": 100.10,
                "stop_loss": 99.96,
                "highest_price_since_entry": 100.10,
            },
            market_constraints={
                "price_increment": "0.05",
                "quantity_increment": "0.000001",
                "minimum_order_quantity": "0.000001",
            },
        ),
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["first_target_price"] == 100.15
    assert data["second_target_price"] == 100.2


def test_exit_all_quantity_never_exceeds_remaining_fractional_quantity():
    result = _decision(
        _payload(
            position={
                "current_price": 99.9,
                "stop_loss": 99.9,
                "highest_price_since_entry": 100,
            }
        )
    )

    assert result.primary_action == ProfitAction.EXIT_ALL
    assert result.actions[0].quantity == 0.333333


@pytest.mark.parametrize(
    ("quantity", "constraints", "message"),
    [
        (
            0.0000015,
            {
                "price_increment": "0.01",
                "quantity_increment": "0.000001",
                "minimum_order_quantity": "0.000001",
            },
            "multiple of quantity_increment",
        ),
        (
            0.005,
            {
                "price_increment": "0.01",
                "quantity_increment": "0.001",
                "minimum_order_quantity": "0.01",
            },
            "at least minimum_order_quantity",
        ),
    ],
)
def test_invalid_position_quantity_for_market_constraints_is_rejected(
    quantity, constraints, message
):
    with pytest.raises(ValidationError, match=message):
        ProfitPlanRequest.model_validate(
            _payload(position={"quantity": quantity}, market_constraints=constraints)
        )


def test_minimum_quantity_must_align_with_quantity_increment():
    with pytest.raises(
        ValidationError, match="minimum_order_quantity must be a multiple"
    ):
        ProfitPlanRequest.model_validate(
            _payload(
                market_constraints={
                    "price_increment": "0.01",
                    "quantity_increment": "0.01",
                    "minimum_order_quantity": "0.015",
                }
            )
        )


@pytest.mark.parametrize("field", ["price_increment", "quantity_increment"])
def test_non_finite_market_increment_is_rejected(field):
    constraints = {
        "price_increment": "0.01",
        "quantity_increment": "0.000001",
        "minimum_order_quantity": "0.000001",
        field: "NaN",
    }
    with pytest.raises(ValidationError):
        ProfitPlanRequest.model_validate(_payload(market_constraints=constraints))


def test_market_constraints_serialize_as_exact_strings_on_all_endpoints():
    constraints = {
        "price_increment": "0.05",
        "quantity_increment": "0.000001",
        "minimum_order_quantity": "0.000001",
    }
    for path in ("/profit/plan", "/profit/monitor", "/profit/exit-signal"):
        response = client.post(
            path,
            headers=AUTH_HEADERS,
            json=_payload(market_constraints=constraints),
        )
        assert response.status_code == 200
        assert response.json()["data"]["market_constraints"] == constraints


def test_trailing_math_uses_decimal_before_compatibility_projection():
    request = ProfitPlanRequest.model_validate(
        _payload(
            position={
                "current_price": 108,
                "stop_loss": 94,
                "highest_price_since_entry": 120,
            }
        )
    )

    assert calculate_raw_trailing_stop(request, Decimal("1.333333")) == 110.4
