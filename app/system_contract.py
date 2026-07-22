from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request

from app.config import PROFIT_AGENT_VERSION, PROFIT_SCHEMA_VERSION


PROFIT_AGENT_TYPE = "profit-agent"

router = APIRouter()


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def request_correlation_id(request: Request) -> str:
    return str(request.state.correlation_id)


def contract_response(
    *,
    status: str,
    correlation_id: str,
    data: Any = None,
    metadata: Optional[Dict[str, Any]] = None,
    error: Optional[Dict[str, Any]] = None,
    confidence_score: Optional[float] = None,
) -> Dict[str, Any]:
    return {
        "status": status,
        "agent_type": PROFIT_AGENT_TYPE,
        "version": PROFIT_AGENT_VERSION,
        "schema_version": PROFIT_SCHEMA_VERSION,
        "timestamp": utc_timestamp(),
        "correlation_id": correlation_id,
        "data": data,
        "metadata": metadata or {},
        "error": error,
        "confidence_score": confidence_score,
    }


@router.get("/version")
def version(request: Request) -> Dict[str, Any]:
    correlation_id = request_correlation_id(request)
    return contract_response(
        status="success",
        correlation_id=correlation_id,
        data={
            "agent_type": PROFIT_AGENT_TYPE,
            "version": PROFIT_AGENT_VERSION,
            "schema_version": PROFIT_SCHEMA_VERSION,
            "api_contract": "multi-agent-trading-api-contract",
        },
        metadata={
            "required_operational_endpoints": ["/health", "/ready", "/version"],
        },
    )


@router.get("/ready")
def ready(request: Request) -> Dict[str, Any]:
    correlation_id = request_correlation_id(request)
    return contract_response(
        status="success",
        correlation_id=correlation_id,
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
            "authentication": "X-API-KEY required on profit endpoints",
        },
        confidence_score=1.0,
    )
