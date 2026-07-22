from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app, raise_server_exceptions=False)
AUTH_HEADERS = {"X-API-KEY": "test-profit-api-key"}


def payload(**overrides):
    value = {
        "position": {
            "symbol": "ACGL",
            "quantity": 10,
            "entry_price": 100,
            "current_price": 108,
            "stop_loss": 96,
            "highest_price_since_entry": 108,
        }
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize("path", ["/profit/plan", "/profit/monitor", "/profit/exit-signal"])
def test_profit_endpoints_require_api_key(path):
    response = client.post(path, json=payload())

    assert response.status_code == 401
    body = response.json()
    assert body["status"] == "error"
    assert body["schema_version"] == "profit-decision.v2"
    assert body["error"] == {
        "code": "authentication_failed",
        "message": "Authentication failed",
    }
    assert body["correlation_id"]
    assert "test-profit-api-key" not in response.text


def test_api_key_comparison_accepts_exact_key_only():
    rejected = client.post(
        "/profit/plan",
        headers={"X-API-KEY": "test-profit-api-key-extra"},
        json=payload(),
    )
    accepted = client.post(
        "/profit/plan",
        headers=AUTH_HEADERS,
        json=payload(),
    )

    assert rejected.status_code == 401
    assert accepted.status_code == 200


def test_openapi_declares_profit_api_key_security():
    schema = client.get("/openapi.json").json()

    scheme = schema["components"]["securitySchemes"]["APIKeyHeader"]
    assert scheme == {"type": "apiKey", "in": "header", "name": "X-API-KEY"}
    assert schema["paths"]["/profit/plan"]["post"]["security"] == [
        {"APIKeyHeader": []}
    ]
    assert "security" not in schema["paths"]["/health"]["get"]


@pytest.mark.parametrize("path", ["/health", "/ready", "/version"])
def test_operational_endpoints_are_open_and_use_one_envelope(path):
    response = client.get(path, headers={"X-Correlation-ID": "ops-correlation-1"})

    assert response.status_code == 200
    body = response.json()
    assert body["version"] == "0.2.0"
    assert body["schema_version"] == "profit-decision.v2"
    assert body["correlation_id"] == "ops-correlation-1"
    assert response.headers["X-Correlation-ID"] == "ops-correlation-1"
    assert set(body) == {
        "status",
        "agent_type",
        "version",
        "schema_version",
        "timestamp",
        "correlation_id",
        "data",
        "metadata",
        "error",
        "confidence_score",
    }


def test_missing_correlation_id_generates_uuid():
    response = client.post("/profit/plan", headers=AUTH_HEADERS, json=payload())

    correlation_id = response.json()["correlation_id"]
    assert len(correlation_id) == 36
    assert response.headers["X-Correlation-ID"] == correlation_id


def test_validation_error_preserves_correlation_without_echoing_payload():
    headers = {**AUTH_HEADERS, "X-Correlation-ID": "validation-corr-1"}
    response = client.post(
        "/profit/plan",
        headers=headers,
        json=payload(unexpected_field="must-not-be-accepted"),
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["correlation_id"] == "validation-corr-1"
    assert "must-not-be-accepted" not in response.text


def test_invalid_schema_version_has_explicit_error_contract():
    response = client.post(
        "/profit/plan",
        headers={**AUTH_HEADERS, "X-Correlation-ID": "schema-corr-1"},
        json=payload(schema_version="profit-decision.v999"),
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_schema_version"
    assert body["correlation_id"] == "schema-corr-1"


def test_malformed_lifecycle_has_explicit_error_contract():
    response = client.post(
        "/profit/plan",
        headers=AUTH_HEADERS,
        json=payload(
            lifecycle={
                "position_id": "account-1:position-1",
                "position_version": 0,
                "remaining_quantity": 10,
            }
        ),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "malformed_lifecycle"


def test_missing_runtime_key_fails_closed(monkeypatch):
    monkeypatch.delenv("PROFIT_AGENT_API_KEY", raising=False)

    response = client.post("/profit/plan", json=payload())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_not_configured"


def test_internal_error_does_not_return_exception_text(monkeypatch):
    def fail(_request):
        raise RuntimeError("database-url-or-secret-must-not-leak")

    monkeypatch.setattr("app.main.build_initial_profit_plan", fail)
    response = client.post(
        "/profit/plan",
        headers={**AUTH_HEADERS, "X-Correlation-ID": "internal-corr-1"},
        json=payload(),
    )

    assert response.status_code == 500
    body = response.json()
    assert body["error"] == {
        "code": "internal_error",
        "message": "An internal error occurred",
    }
    assert body["correlation_id"] == "internal-corr-1"
    assert "database-url-or-secret-must-not-leak" not in response.text


def test_production_import_fails_without_api_key():
    project_root = Path(__file__).resolve().parents[1]
    command = [sys.executable, "-c", "import app.main"]
    environment = {
        "APP_ENV": "production",
        "PYTHONPATH": str(project_root),
    }
    result = subprocess.run(
        command,
        cwd=str(project_root),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "PROFIT_AGENT_API_KEY is required in production" in result.stderr
