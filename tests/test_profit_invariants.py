import math

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.models import ProfitAction, ProfitPlanRequest, ProfitPosition
from app.service import (
    HIGHEST_PRICE_FALLBACK_WARNING,
    build_profit_plan,
    calculate_raw_trailing_stop,
    detect_trailing_stop_breach,
)


client = TestClient(app)
AUTH_HEADERS = {"X-API-KEY": "test-profit-api-key"}


def _request(*, current_price=108.0, peak=120.0, **overrides):
    position = {
        "symbol": "ACGL",
        "quantity": 10,
        "entry_price": 100.0,
        "current_price": current_price,
        "stop_loss": 94.0,
        "highest_price_since_entry": peak,
    }
    position.update(overrides.pop("position", {}))
    return ProfitPlanRequest(position=ProfitPosition(**position), **overrides)


def test_hard_stop_breach_has_highest_safety_priority():
    result = build_profit_plan(_request(current_price=94.0, peak=120.0))

    assert result.primary_action == ProfitAction.EXIT_ALL
    assert result.trigger == "hard_stop_loss_breach"
    assert result.recommended_stop == 94.0
    assert result.requires_risk_approval is True
    assert result.advisory_only is True
    assert result.current_r_multiple is None


def test_legacy_exit_flag_cannot_suppress_hard_stop_detection():
    result = build_profit_plan(
        _request(current_price=94.0, peak=120.0, exit_on_stop_breach=False)
    )

    assert result.primary_action == ProfitAction.EXIT_ALL
    assert result.trigger == "hard_stop_loss_breach"


def test_trailing_stop_breach_is_not_discarded():
    request = _request(current_price=108.0, peak=120.0)

    raw_stop = calculate_raw_trailing_stop(request, current_r=8 / 6)
    result = build_profit_plan(request)

    assert raw_stop == pytest.approx(110.40)
    assert detect_trailing_stop_breach(request, raw_stop) is True
    assert result.primary_action == ProfitAction.EXIT_ALL
    assert result.trigger == "trailing_stop_breach"
    assert result.recommended_stop == 110.40
    assert result.actions[0].quantity == 10
    assert result.requires_risk_approval is True
    assert result.advisory_only is True


@pytest.mark.parametrize(
    ("current_price", "expected_breach"),
    [
        pytest.param(110.40, True, id="equal-to-trailing-stop"),
        pytest.param(110.41, False, id="one-cent-above-trailing-stop"),
    ],
)
def test_trailing_stop_boundary(current_price, expected_breach):
    result = build_profit_plan(_request(current_price=current_price, peak=120.0))

    assert (result.trigger == "trailing_stop_breach") is expected_breach
    assert (result.primary_action == ProfitAction.EXIT_ALL) is expected_breach


@pytest.mark.parametrize(
    ("position_override", "message"),
    [
        ({"highest_price_since_entry": 107.99}, "at least current_price"),
        ({"highest_price_since_entry": 99.99}, "at least entry_price"),
        ({"stop_loss": 100.0}, "below entry_price"),
        ({"stop_loss": 101.0}, "below entry_price"),
    ],
)
def test_invalid_long_price_relationships_are_rejected(position_override, message):
    with pytest.raises(ValidationError, match=message):
        _request(**{"position": position_override})


def test_inconsistent_risk_per_share_rejected_by_default(monkeypatch):
    monkeypatch.delenv("PROFIT_RISK_MISMATCH_POLICY", raising=False)

    with pytest.raises(ValidationError, match="risk_per_share does not match"):
        _request(position={"risk_per_share": 5.0})


def test_risk_mismatch_warn_policy_preserves_warning(monkeypatch):
    monkeypatch.setenv("PROFIT_RISK_MISMATCH_POLICY", "warn")

    result = build_profit_plan(_request(position={"risk_per_share": 5.0}))

    assert any("policy=warn" in warning for warning in result.warnings)


def test_risk_mismatch_recalculate_policy_uses_derived_risk(monkeypatch):
    monkeypatch.setenv("PROFIT_RISK_MISMATCH_POLICY", "recalculate")
    request = _request(position={"risk_per_share": 5.0})

    result = build_profit_plan(request)

    assert request.position.risk_per_share == 6.0
    assert result.current_r_multiple == pytest.approx(8 / 6, abs=1e-4)
    assert any("policy=recalculate" in warning for warning in result.warnings)


@pytest.mark.parametrize("invalid_number", [math.nan, math.inf, -math.inf])
def test_nan_and_infinity_are_rejected(invalid_number):
    with pytest.raises(ValidationError):
        _request(position={"current_price": invalid_number})


@pytest.mark.parametrize(
    "payload",
    [
        {"unexpected": True},
        {"position": {"unexpected": True}},
    ],
)
def test_unknown_fields_are_rejected(payload):
    body = {
        "position": {
            "symbol": "ACGL",
            "quantity": 10,
            "entry_price": 100,
            "current_price": 108,
            "stop_loss": 94,
            "highest_price_since_entry": 120,
        }
    }
    if "position" in payload:
        body["position"].update(payload["position"])
    else:
        body.update(payload)

    response = client.post("/profit/plan", headers=AUTH_HEADERS, json=body)

    assert response.status_code == 422
    payload = response.json()
    assert payload["error"]["code"] == "validation_error"
    assert any(
        error["type"] == "extra_forbidden"
        for error in payload["metadata"]["validation_errors"]
    )


@pytest.mark.parametrize(
    "symbol",
    ["", " ACGL", "ACGL ", "AC GL", "ACGL/US", "@ACGL"],
)
def test_invalid_symbols_are_rejected(symbol):
    with pytest.raises(ValidationError):
        _request(position={"symbol": symbol})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("first_take_profit_r", 3.0),
        ("first_take_profit_r", 3.1),
    ],
)
def test_invalid_target_ordering_is_rejected(field, value):
    with pytest.raises(ValidationError, match="second_take_profit_r must be greater"):
        _request(**{field: value, "second_take_profit_r": 3.0})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quantity", 0),
        ("quantity", -1),
        ("risk_per_share", 0),
        ("risk_per_share", -1),
    ],
)
def test_non_positive_position_values_are_rejected(field, value):
    with pytest.raises(ValidationError):
        _request(position={field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("partial_exit_pct", 0),
        ("partial_exit_pct", 1.000001),
        ("trailing_stop_pct", 0),
        ("trailing_stop_pct", 1.000001),
    ],
)
def test_percentage_boundaries_are_enforced(field, value):
    with pytest.raises(ValidationError):
        _request(**{field: value})


@pytest.mark.parametrize("peak", [pytest.param(None, id="explicit-null")])
def test_explicit_null_peak_warns(peak):
    result = build_profit_plan(_request(peak=peak))

    assert HIGHEST_PRICE_FALLBACK_WARNING in result.warnings


def test_missing_peak_warns():
    position = ProfitPosition(
        symbol="ACGL",
        quantity=10,
        entry_price=100,
        current_price=108,
        stop_loss=94,
    )

    result = build_profit_plan(ProfitPlanRequest(position=position))

    assert HIGHEST_PRICE_FALLBACK_WARNING in result.warnings


@pytest.mark.parametrize(
    ("current_price", "peak", "expected_trigger"),
    [
        pytest.param(94.0, 120.0, "hard_stop_loss_breach", id="hard-stop"),
        pytest.param(108.0, 120.0, "trailing_stop_breach", id="trailing-stop"),
    ],
)
def test_request_warnings_survive_early_return(current_price, peak, expected_trigger):
    result = build_profit_plan(
        _request(
            current_price=current_price,
            peak=peak,
            warnings=["upstream data-quality warning"],
        )
    )

    assert result.trigger == expected_trigger
    assert "upstream data-quality warning" in result.warnings


@pytest.mark.parametrize(
    "path",
    ["/profit/plan", "/profit/monitor", "/profit/exit-signal"],
)
def test_all_profit_endpoints_detect_trailing_stop_breach(path):
    response = client.post(
        path,
        headers=AUTH_HEADERS,
        json={
            "position": {
                "symbol": "ACGL",
                "quantity": 10,
                "entry_price": 100,
                "current_price": 108,
                "stop_loss": 94,
                "highest_price_since_entry": 120,
            },
            "trailing_stop_pct": 0.08,
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["primary_action"] == "exit_all"
    assert data["trigger"] == "trailing_stop_breach"
    assert data["recommended_stop"] == 110.4
    assert data["requires_risk_approval"] is True
    assert data["advisory_only"] is True
