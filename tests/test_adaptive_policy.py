from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.models import ProfitAction, ProfitPlanRequest
from app.service import build_profit_plan


client = TestClient(app)
AUTH_HEADERS = {"X-API-KEY": "test-profit-api-key"}
GOOD_QUALITY = {
    "market_price_fresh": True,
    "peak_history_complete": True,
    "position_version_current": True,
}


def _payload(*, current_price=108, peak=108, context=None, quality=GOOD_QUALITY):
    payload = {
        "schema_version": "profit-decision.v2",
        "position": {
            "symbol": "ACGL",
            "quantity": 10,
            "entry_price": 100,
            "current_price": current_price,
            "stop_loss": 94,
            "highest_price_since_entry": peak,
        },
        "first_take_profit_r": 2,
        "second_take_profit_r": 3,
        "partial_exit_pct": 0.30,
        "trailing_stop_pct": 0.08,
    }
    if context is not None:
        payload["market_context"] = context
    if quality is not None:
        payload["data_quality"] = quality
    return payload


def _context(**overrides):
    context = {
        "regime": "BULL",
        "risk_level": "MEDIUM",
        "atr_pct": 0.025,
        "volatility_percentile": 65,
        "trend_strength": 0.78,
        "volume_strength": 0.70,
        "holding_days": 12,
        "upcoming_event_risk": False,
    }
    context.update(overrides)
    return context


def _decision(payload):
    return build_profit_plan(ProfitPlanRequest.model_validate(payload))


def test_bull_strong_trend_widens_trailing_and_reduces_partial_exit():
    result = _decision(_payload(context=_context()))

    assert result.policy_source == "deterministic_adaptive_v1"
    assert result.base_trailing_stop_pct == 0.08
    assert result.adjusted_trailing_stop_pct == 0.10
    assert result.adjusted_partial_exit_pct == 0.225
    assert result.adjustment_reasons == ["bull regime with strong trend"]


def test_volatile_policy_uses_atr_and_increases_partial_exit():
    result = _decision(
        _payload(
            context=_context(
                regime="VOLATILE",
                atr_pct=0.06,
                volatility_percentile=90,
                trend_strength=0.30,
            )
        )
    )

    assert result.adjusted_trailing_stop_pct == 0.15
    assert result.adjusted_partial_exit_pct == 0.375
    assert "high volatility" in result.adjustment_reasons


def test_bear_weak_trend_reduces_targets_and_tightens_stop():
    result = _decision(_payload(context=_context(regime="BEAR", trend_strength=0.20)))

    assert result.adjusted_first_take_profit_r == 1.6
    assert result.adjusted_second_take_profit_r == 2.4
    assert result.adjusted_trailing_stop_pct == 0.06
    assert result.adjusted_partial_exit_pct == 0.375
    assert result.adjustment_reasons == ["bear regime with weak trend"]


def test_hard_stop_preempts_adaptive_policy():
    result = _decision(
        _payload(
            current_price=94,
            peak=120,
            context=_context(regime="BULL", trend_strength=1),
        )
    )

    assert result.primary_action == ProfitAction.EXIT_ALL
    assert result.trigger == "hard_stop_loss_breach"
    assert result.recommended_stop == 94
    assert result.policy_source == "static_v1"


def test_base_trailing_breach_cannot_be_hidden_by_wider_adaptive_stop():
    result = _decision(
        _payload(
            current_price=108,
            peak=120,
            context=_context(regime="BULL", trend_strength=1),
        )
    )

    assert result.primary_action == ProfitAction.EXIT_ALL
    assert result.trigger == "trailing_stop_breach"
    assert result.recommended_stop == 110.4
    assert result.policy_source == "static_v1"


def test_tighter_adaptive_stop_is_checked_after_base_safety():
    result = _decision(
        _payload(
            current_price=112,
            peak=120,
            context=_context(upcoming_event_risk=True, trend_strength=0.50),
        )
    )

    assert result.primary_action == ProfitAction.EXIT_ALL
    assert result.trigger == "trailing_stop_breach"
    assert result.recommended_stop == 112.8
    assert result.adjusted_trailing_stop_pct == 0.06
    assert "upcoming event risk" in result.adjustment_reasons


@pytest.mark.parametrize(
    ("quality_override", "warning"),
    [
        ({"market_price_fresh": False}, "market data is stale"),
        ({"peak_history_complete": False}, "peak price history is incomplete"),
        ({"position_version_current": False}, "position version is stale"),
    ],
)
def test_explicit_bad_data_quality_blocks_action(quality_override, warning):
    quality = {**GOOD_QUALITY, **quality_override}
    result = _decision(_payload(context=_context(), quality=quality))

    assert result.primary_action == ProfitAction.REVIEW
    assert result.decision_status == "blocked"
    assert result.requires_risk_approval is False
    assert result.decision_id is None
    assert any(warning in value for value in result.warnings)


def test_emergency_halt_has_priority_over_price_rules():
    quality = {**GOOD_QUALITY, "emergency_halt_active": True}
    result = _decision(_payload(current_price=94, peak=120, quality=quality))

    assert result.primary_action == ProfitAction.REVIEW
    assert result.trigger == "emergency_halt"
    assert result.decision_status == "blocked"


def test_market_context_without_freshness_evidence_is_blocked():
    result = _decision(_payload(context=_context(), quality=None))

    assert result.primary_action == ProfitAction.REVIEW
    assert result.data_quality["market_price_fresh"] is False


def test_stale_observed_at_is_blocked(monkeypatch):
    monkeypatch.setenv("PROFIT_MARKET_DATA_MAX_AGE_SECONDS", "120")
    observed_at = datetime.now(timezone.utc) - timedelta(seconds=121)
    result = _decision(
        _payload(
            context=_context(observed_at=observed_at.isoformat()),
            quality=None,
        )
    )

    assert result.primary_action == ProfitAction.REVIEW
    assert result.trigger == "data_quality_block"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("atr_pct", 0),
        ("volatility_percentile", 101),
        ("trend_strength", -0.01),
        ("volume_strength", 1.01),
        ("holding_days", -1),
        ("unexpected", True),
    ],
)
def test_invalid_market_context_is_rejected(field, value):
    with pytest.raises(ValidationError):
        ProfitPlanRequest.model_validate(_payload(context=_context(**{field: value})))


@pytest.mark.parametrize(
    "path",
    ["/profit/plan", "/profit/monitor", "/profit/exit-signal"],
)
def test_policy_explanation_is_returned_by_every_endpoint(path):
    response = client.post(
        path,
        headers=AUTH_HEADERS,
        json=_payload(context=_context()),
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["policy_source"] == "deterministic_adaptive_v1"
    assert data["base_trailing_stop_pct"] == 0.08
    assert data["adjusted_trailing_stop_pct"] == 0.10
    assert data["adjustment_reasons"] == ["bull regime with strong trend"]
