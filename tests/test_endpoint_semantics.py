from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)
AUTH_HEADERS = {"X-API-KEY": "test-profit-api-key"}


def _payload(*, current_price=106, peak=None, lifecycle=None):
    body = {
        "schema_version": "profit-decision.v2",
        "position": {
            "symbol": "ACGL",
            "quantity": 10,
            "entry_price": 100,
            "current_price": current_price,
            "stop_loss": 96,
            "highest_price_since_entry": peak or current_price,
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


def test_plan_returns_initial_targets_and_policies():
    response = client.post(
        "/profit/plan",
        headers=AUTH_HEADERS,
        json=_payload(),
    )

    assert response.status_code == 200
    body = response.json()
    data = body["data"]
    assert body["metadata"]["endpoint_semantics"] == "initial_profit_plan"
    assert data["initial_stop"] == 96
    assert data["first_target_price"] == 108
    assert data["second_target_price"] == 112
    assert data["trailing_policy"] == {
        "activation_r": 1,
        "trailing_stop_pct": 0.08,
        "reference": "highest_price_since_entry",
        "breach_at_or_below": True,
    }
    assert data["partial_exit_policy"] == {
        "first_target_r": 2,
        "second_target_r": 3,
        "partial_exit_pct": 0.3,
    }


def test_monitor_returns_stage_and_database_target_status():
    response = client.post(
        "/profit/monitor",
        headers=AUTH_HEADERS,
        json=_payload(current_price=108, lifecycle={}),
    )

    assert response.status_code == 200
    body = response.json()
    data = body["data"]
    assert body["metadata"]["endpoint_semantics"] == "position_monitor"
    assert data["current_r"] == 2
    assert data["profit_stage"] == "first_target_reached"
    assert data["target_status"] == {
        "lifecycle_available": True,
        "first_target_reached": True,
        "first_target_executed": False,
        "second_target_reached": False,
        "second_target_executed": False,
        "remaining_quantity": 10,
    }


def test_exit_signal_is_compact_and_immediate_for_trailing_breach():
    response = client.post(
        "/profit/exit-signal",
        headers=AUTH_HEADERS,
        json=_payload(current_price=108, peak=120),
    )

    assert response.status_code == 200
    body = response.json()
    data = body["data"]
    assert body["metadata"]["endpoint_semantics"] == "risk_gate_exit_signal"
    assert data["should_exit"] is True
    assert data["exit_type"] == "trailing_stop_breach"
    assert data["urgency"] == "immediate"
    assert data["recommended_quantity"] == 10
    assert data["recommended_stop"] == 110.4
    assert data["requires_risk_approval"] is True
    assert data["advisory_only"] is True
    assert "actions" not in data


def test_exit_signal_returns_no_exit_for_hold():
    response = client.post(
        "/profit/exit-signal",
        headers=AUTH_HEADERS,
        json=_payload(current_price=101),
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["should_exit"] is False
    assert data["exit_type"] is None
    assert data["urgency"] == "none"
    assert data["recommended_quantity"] == 0
    assert data["requires_risk_approval"] is False


def test_openapi_exposes_distinct_response_contracts():
    paths = client.get("/openapi.json").json()["paths"]
    schemas = {
        path: paths[path]["post"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        for path in ("/profit/plan", "/profit/monitor", "/profit/exit-signal")
    }

    assert len(set(schemas.values())) == 3
    assert "ProfitInitialPlanData" in schemas["/profit/plan"]
    assert "ProfitMonitorData" in schemas["/profit/monitor"]
    assert "ProfitExitSignalData" in schemas["/profit/exit-signal"]