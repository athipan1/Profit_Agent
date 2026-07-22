from __future__ import annotations

import io
import json
import logging
import time

from fastapi.testclient import TestClient

from app.main import app
from app.observability import LOGGER, JsonFormatter
from app.runner import main as run_server
from app.runtime_guards import FixedWindowRateLimiter, profit_rate_limiter


client = TestClient(app, raise_server_exceptions=False)
AUTH_HEADERS = {"X-API-KEY": "test-profit-api-key"}


def _payload(**overrides):
    payload = {
        "position": {
            "symbol": "ACGL",
            "quantity": 10,
            "entry_price": 100,
            "current_price": 108,
            "stop_loss": 96,
            "highest_price_since_entry": 108,
        }
    }
    payload.update(overrides)
    return payload


def test_request_body_limit_returns_versioned_safe_error(monkeypatch):
    monkeypatch.setenv("PROFIT_MAX_REQUEST_BODY_BYTES", "64")
    profit_rate_limiter.reset()

    response = client.post(
        "/profit/plan",
        headers={**AUTH_HEADERS, "X-Correlation-ID": "body-limit-1"},
        json=_payload(padding="x" * 128),
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_body_too_large"
    assert response.json()["correlation_id"] == "body-limit-1"


def test_rate_limit_is_fail_closed_and_has_retry_after(monkeypatch):
    monkeypatch.setenv("PROFIT_RATE_LIMIT_PER_MINUTE", "1")
    profit_rate_limiter.reset()
    try:
        accepted = client.post("/profit/plan", headers=AUTH_HEADERS, json=_payload())
        rejected = client.post("/profit/plan", headers=AUTH_HEADERS, json=_payload())
    finally:
        profit_rate_limiter.reset()

    assert accepted.status_code == 200
    assert rejected.status_code == 429
    assert rejected.headers["Retry-After"] == "60"
    assert rejected.json()["error"]["code"] == "rate_limit_exceeded"


def test_request_timeout_does_not_expose_exception(monkeypatch):
    def slow_plan(_payload):
        time.sleep(0.05)
        raise RuntimeError("secret-after-timeout")

    monkeypatch.setenv("PROFIT_REQUEST_TIMEOUT_SECONDS", "0.005")
    monkeypatch.setattr("app.main.build_initial_profit_plan", slow_plan)
    profit_rate_limiter.reset()

    response = client.post(
        "/profit/plan",
        headers={**AUTH_HEADERS, "X-Correlation-ID": "timeout-1"},
        json=_payload(),
    )

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "request_timeout"
    assert "secret-after-timeout" not in response.text


def test_metrics_endpoint_exposes_required_low_cardinality_metrics():
    profit_rate_limiter.reset()
    client.post("/profit/plan", headers=AUTH_HEADERS, json=_payload())

    response = client.get("/metrics")

    assert response.status_code == 200
    for name in (
        "profit_requests_total",
        "profit_validation_failures_total",
        "profit_stop_breaches_total",
        "profit_trailing_stop_breaches_total",
        "profit_partial_exit_decisions_total",
        "profit_duplicate_decisions_total",
        "profit_peak_fallback_total",
        "profit_request_duration_seconds",
    ):
        assert name in response.text
    assert "symbol=" not in response.text


def test_decision_json_log_contains_trace_fields_but_not_api_key():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    LOGGER.addHandler(handler)
    profit_rate_limiter.reset()
    try:
        response = client.post(
            "/profit/plan",
            headers={**AUTH_HEADERS, "X-Correlation-ID": "log-correlation-1"},
            json=_payload(),
        )
    finally:
        LOGGER.removeHandler(handler)

    assert response.status_code == 200
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    decision = next(row for row in records if row["event"] == "profit_decision_created")
    assert decision["correlation_id"] == "log-correlation-1"
    assert decision["symbol"] == "ACGL"
    assert decision["advisory_only"] is True
    assert "test-profit-api-key" not in stream.getvalue()


def test_fixed_window_limiter_resets_on_new_window():
    limiter = FixedWindowRateLimiter()

    assert limiter.allow("client", 1, now=0) is True
    assert limiter.allow("client", 1, now=1) is False
    assert limiter.allow("client", 1, now=60) is True
    limiter.reset()
    assert limiter.allow("client", 1, now=1) is True


def test_runner_disables_access_and_server_headers(monkeypatch):
    captured = {}

    monkeypatch.setenv("PROFIT_AGENT_HOST", "127.0.0.1")
    monkeypatch.setenv("PROFIT_AGENT_PORT", "9011")
    monkeypatch.setenv("PROFIT_AGENT_WORKERS", "2")
    monkeypatch.setenv("PROFIT_AGENT_LOG_LEVEL", "WARNING")
    monkeypatch.setattr(
        "app.runner.uvicorn.run", lambda *args, **kwargs: captured.update(kwargs)
    )

    run_server()

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9011
    assert captured["workers"] == 2
    assert captured["access_log"] is False
    assert captured["server_header"] is False
    assert captured["timeout_graceful_shutdown"] == 30