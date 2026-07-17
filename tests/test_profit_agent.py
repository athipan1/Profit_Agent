import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import ProfitAction, ProfitPlanRequest, ProfitPosition
from app.service import HIGHEST_PRICE_FALLBACK_WARNING, build_profit_plan


client = TestClient(app)


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["status"] == "healthy"


def test_stop_breach_exits_all():
    result = build_profit_plan(
        ProfitPlanRequest(
            position=ProfitPosition(
                symbol="ACGL",
                quantity=10,
                entry_price=100,
                current_price=94,
                stop_loss=95,
            )
        )
    )
    assert result.primary_action == ProfitAction.EXIT_ALL
    assert result.actions[0].quantity == 10
    assert HIGHEST_PRICE_FALLBACK_WARNING in result.warnings


def test_first_take_profit_partial_exit():
    result = build_profit_plan(
        ProfitPlanRequest(
            position=ProfitPosition(
                symbol="ADBE",
                quantity=20,
                entry_price=100,
                current_price=120,
                stop_loss=90,
            ),
            first_take_profit_r=2.0,
            partial_exit_pct=0.30,
        )
    )
    assert result.current_r_multiple == 2.0
    assert result.primary_action == ProfitAction.PARTIAL_EXIT
    assert any(action.action == ProfitAction.PARTIAL_EXIT and action.quantity == 6 for action in result.actions)


def test_break_even_or_trailing_stop_move_stop():
    result = build_profit_plan(
        ProfitPlanRequest(
            position=ProfitPosition(
                symbol="CINF",
                quantity=30,
                entry_price=100,
                current_price=112,
                stop_loss=94,
                highest_price_since_entry=120,
            ),
            trailing_stop_pct=0.08,
            break_even_trigger_r=1.0,
        )
    )
    assert any(action.action == ProfitAction.MOVE_STOP for action in result.actions)
    move_stop = next(action for action in result.actions if action.action == ProfitAction.MOVE_STOP)
    assert move_stop.recommended_stop == 110.4
    assert HIGHEST_PRICE_FALLBACK_WARNING not in result.warnings


def test_hold_when_no_profit_rule_triggered():
    result = build_profit_plan(
        ProfitPlanRequest(
            position=ProfitPosition(
                symbol="MSFT",
                quantity=5,
                entry_price=100,
                current_price=104,
                stop_loss=95,
            )
        )
    )
    assert result.primary_action == ProfitAction.HOLD


def test_profit_plan_endpoint():
    response = client.post(
        "/profit/plan",
        json={
            "position": {
                "symbol": "ADBE",
                "quantity": 20,
                "entry_price": 100,
                "current_price": 120,
                "stop_loss": 90,
                "highest_price_since_entry": 125,
            },
            "first_take_profit_r": 2.0,
            "partial_exit_pct": 0.30,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["data"]["primary_action"] == "partial_exit"
    assert payload["data"]["symbol"] == "ADBE"
    assert HIGHEST_PRICE_FALLBACK_WARNING not in payload["data"]["warnings"]


@pytest.mark.parametrize("path", ["/profit/plan", "/profit/monitor", "/profit/exit-signal"])
@pytest.mark.parametrize(
    "peak_payload",
    [
        pytest.param({}, id="field-omitted"),
        pytest.param({"highest_price_since_entry": None}, id="explicit-null"),
    ],
)
def test_profit_endpoints_warn_when_highest_price_is_unavailable(path, peak_payload):
    response = client.post(
        path,
        json={
            "position": {
                "symbol": "ADBE",
                "quantity": 20,
                "entry_price": 100,
                "current_price": 120,
                "stop_loss": 90,
                **peak_payload,
            },
            "first_take_profit_r": 2.0,
            "partial_exit_pct": 0.30,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["error"] is None
    assert payload["data"]["symbol"] == "ADBE"
    assert HIGHEST_PRICE_FALLBACK_WARNING in payload["data"]["warnings"]
