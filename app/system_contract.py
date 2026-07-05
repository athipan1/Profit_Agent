from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter

PROFIT_AGENT_TYPE = "profit-agent"
PROFIT_AGENT_VERSION = "0.1.0"
SCHEMA_VERSION = "1.0"

router = APIRouter()


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def contract_response(
    *,
    status: str,
    data: Dict[str, Any] | None = None,
    metadata: Dict[str, Any] | None = None,
    error: Dict[str, Any] | None = None,
    confidence_score: float | None = None,
) -> Dict[str, Any]:
    return {
        "status": status,
        "agent_type": PROFIT_AGENT_TYPE,
        "version": PROFIT_AGENT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "timestamp": utc_timestamp(),
        "correlation_id": None,
        "data": data,
        "metadata": metadata or {},
        "error": error,
        "confidence_score": confidence_score,
    }


@router.get("/version")
def version() -> Dict[str, Any]:
    return contract_response(
        status="success",
        data={
            "agent_type": PROFIT_AGENT_TYPE,
            "version": PROFIT_AGENT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "api_contract": "multi-agent-trading-api-contract",
        },
        metadata={
            "required_operational_endpoints": ["/health", "/ready", "/version"],
        },
    )


@router.get("/ready")
def ready() -> Dict[str, Any]:
    return contract_response(
        status="success",
        data={
            "ready": True,
            "plan_endpoint": "/profit/plan",
            "monitor_endpoint": "/profit/monitor",
            "exit_signal_endpoint": "/profit/exit-signal",
            "supported_actions": [
                "hold",
                "move_stop",
                "partial_exit",
                "exit_all",
                "review",
            ],
        },
        metadata={
            "contract_source": "profit-agent-runtime-contract",
        },
        confidence_score=1.0,
    )
