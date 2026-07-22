from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.config import (
    max_request_body_bytes,
    profit_agent_log_level,
    rate_limit_per_minute,
    request_timeout_seconds,
    validate_runtime_configuration,
)
from app.models import (
    HealthData,
    ProfitExitSignalData,
    ProfitInitialPlanData,
    ProfitMonitorData,
    ProfitPlanRequest,
    StandardAgentResponse,
    StrictModel,
)
from app.security import (
    ProfitAuthenticationError,
    ProfitConfigurationError,
    require_profit_api_key,
)
from app.observability import (
    PROFIT_DUPLICATE_DECISIONS,
    PROFIT_PARTIAL_EXIT_DECISIONS,
    PROFIT_PEAK_FALLBACKS,
    PROFIT_REQUEST_DURATION,
    PROFIT_REQUESTS,
    PROFIT_STOP_BREACHES,
    PROFIT_TRAILING_STOP_BREACHES,
    PROFIT_VALIDATION_FAILURES,
    configure_json_logging,
    log_event,
)
from app.runtime_guards import profit_rate_limiter
from app.services.exit_signal import build_exit_signal
from app.services.profit_monitor import build_profit_monitor
from app.services.profit_planner import build_initial_profit_plan
from app.system_contract import contract_response, router as system_contract_router


CORRELATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

validate_runtime_configuration()
configure_json_logging(profit_agent_log_level())

app = FastAPI(
    title="Profit Agent",
    description="Profit-taking advisory service for the multi-agent trading system.",
    version="0.2.0",
)
app.include_router(system_contract_router)


def _correlation_id(request: Request) -> str:
    return str(request.state.correlation_id)


@app.middleware("http")
async def runtime_guard_middleware(request: Request, call_next):
    started_at = time.perf_counter()
    supplied = (request.headers.get("X-Correlation-ID") or "").strip()
    request.state.correlation_id = (
        supplied if CORRELATION_ID_PATTERN.fullmatch(supplied) else str(uuid4())
    )
    endpoint = request.url.path
    response: Response

    if endpoint.startswith("/profit/"):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError:
                response = _error_response(
                    request,
                    status_code=400,
                    code="invalid_content_length",
                    message="Content-Length must be an integer",
                )
                return _finalize_response(request, response, endpoint, started_at)
            if declared_size > max_request_body_bytes():
                response = _error_response(
                    request,
                    status_code=413,
                    code="request_body_too_large",
                    message="Request body exceeds the configured limit",
                )
                return _finalize_response(request, response, endpoint, started_at)

        body = await request.body()
        if len(body) > max_request_body_bytes():
            response = _error_response(
                request,
                status_code=413,
                code="request_body_too_large",
                message="Request body exceeds the configured limit",
            )
            return _finalize_response(request, response, endpoint, started_at)

        client_key = request.client.host if request.client is not None else "unknown"
        if not profit_rate_limiter.allow(client_key, rate_limit_per_minute()):
            response = _error_response(
                request,
                status_code=429,
                code="rate_limit_exceeded",
                message="Profit request rate limit exceeded",
            )
            response.headers["Retry-After"] = "60"
            return _finalize_response(request, response, endpoint, started_at)

    try:
        response = await asyncio.wait_for(
            call_next(request), timeout=request_timeout_seconds()
        )
    except TimeoutError:
        response = _error_response(
            request,
            status_code=504,
            code="request_timeout",
            message="Profit request exceeded the configured timeout",
        )
    return _finalize_response(request, response, endpoint, started_at)


def _finalize_response(
    request: Request,
    response: Response,
    endpoint: str,
    started_at: float,
) -> Response:
    duration = time.perf_counter() - started_at
    response.headers["X-Correlation-ID"] = request.state.correlation_id
    PROFIT_REQUESTS.labels(endpoint=endpoint, status=str(response.status_code)).inc()
    PROFIT_REQUEST_DURATION.labels(endpoint=endpoint).observe(duration)
    log_event(
        "profit_request_completed",
        correlation_id=_correlation_id(request),
        endpoint=endpoint,
        method=request.method,
        status_code=response.status_code,
        duration_seconds=round(duration, 6),
    )
    return response


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    metadata: Dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=contract_response(
            status="error",
            correlation_id=_correlation_id(request),
            data=None,
            metadata=metadata,
            error={"code": code, "message": message},
            confidence_score=None,
        ),
    )


@app.exception_handler(ProfitAuthenticationError)
async def authentication_error_handler(
    request: Request, exc: ProfitAuthenticationError
) -> JSONResponse:
    log_event(
        "profit_authentication_failed",
        level=logging.WARNING,
        correlation_id=_correlation_id(request),
        endpoint=request.url.path,
    )
    return _error_response(
        request,
        status_code=401,
        code="authentication_failed",
        message="Authentication failed",
    )


@app.exception_handler(ProfitConfigurationError)
async def configuration_error_handler(
    request: Request, exc: ProfitConfigurationError
) -> JSONResponse:
    return _error_response(
        request,
        status_code=503,
        code="service_not_configured",
        message="Profit Agent authentication is not configured",
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    PROFIT_VALIDATION_FAILURES.inc()
    errors = exc.errors()
    locations = [tuple(str(part) for part in item.get("loc") or ()) for item in errors]
    invalid_schema = any("schema_version" in location for location in locations)
    malformed_lifecycle = any("lifecycle" in location for location in locations)
    code = (
        "invalid_schema_version"
        if invalid_schema
        else "malformed_lifecycle"
        if malformed_lifecycle
        else "validation_error"
    )
    status_code = 400 if invalid_schema else 422
    log_event(
        "profit_validation_failed",
        level=logging.WARNING,
        correlation_id=_correlation_id(request),
        endpoint=request.url.path,
        error_code=code,
    )
    return _error_response(
        request,
        status_code=status_code,
        code=code,
        message=(
            "Unsupported Profit API schema version"
            if invalid_schema
            else "Profit lifecycle payload is malformed"
            if malformed_lifecycle
            else "Request validation failed"
        ),
        metadata={
            "validation_errors": [
                {
                    "location": list(location),
                    "type": str(item.get("type") or "validation_error"),
                }
                for location, item in zip(locations, errors)
            ]
        },
    )


@app.exception_handler(Exception)
async def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
    log_event(
        "profit_internal_error",
        level=logging.ERROR,
        correlation_id=_correlation_id(request),
        endpoint=request.url.path,
        exception_type=type(exc).__name__,
    )
    return _error_response(
        request,
        status_code=500,
        code="internal_error",
        message="An internal error occurred",
    )


@app.get("/health")
def health(request: Request) -> Dict[str, Any]:
    return contract_response(
        status="success",
        correlation_id=_correlation_id(request),
        data=HealthData().model_dump(mode="json"),
        metadata={"authentication_required": False},
        confidence_score=1.0,
    )


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def _profit_response(
    request: Request,
    payload: ProfitPlanRequest,
    data: StrictModel,
    *,
    endpoint_semantics: str,
) -> Dict[str, Any]:
    actions = getattr(data, "actions", [])
    confidence = max(
        (item.confidence_score for item in actions),
        default=(0.90 if getattr(data, "should_exit", False) else 0.65),
    )
    primary_value = getattr(data, "primary_action", "hold")
    primary_action = str(getattr(primary_value, "value", primary_value))
    trigger = getattr(data, "trigger", None)
    if trigger == "hard_stop_loss_breach":
        PROFIT_STOP_BREACHES.inc()
    if trigger == "trailing_stop_breach":
        PROFIT_TRAILING_STOP_BREACHES.inc()
    if primary_action == "partial_exit":
        PROFIT_PARTIAL_EXIT_DECISIONS.inc()
    if payload.position.highest_price_since_entry_inferred:
        PROFIT_PEAK_FALLBACKS.inc()
    lifecycle = payload.lifecycle
    current_r = getattr(data, "current_r_multiple", None)
    if (
        lifecycle is not None
        and getattr(data, "decision_id", None) is None
        and current_r is not None
        and (
            lifecycle.second_target_executed
            or (
                lifecycle.first_target_executed
                and current_r >= payload.first_take_profit_r
            )
        )
    ):
        PROFIT_DUPLICATE_DECISIONS.inc()
    selected_action = next(
        (item for item in actions if str(item.action.value) == primary_action),
        actions[0] if actions else None,
    )
    log_event(
        "profit_decision_created",
        correlation_id=_correlation_id(request),
        decision_id=getattr(data, "decision_id", None),
        position_id=lifecycle.position_id if lifecycle is not None else None,
        symbol=payload.position.symbol.upper(),
        action=primary_action,
        reason=getattr(selected_action, "reason", None),
        advisory_only=True,
    )
    return contract_response(
        status="success",
        correlation_id=_correlation_id(request),
        data=data.model_dump(mode="json"),
        metadata={
            "advisory_only": True,
            "endpoint_semantics": endpoint_semantics,
            "request_schema_version": payload.schema_version or "profit-plan.v1",
        },
        confidence_score=confidence,
    )


@app.post(
    "/profit/plan",
    response_model=StandardAgentResponse[ProfitInitialPlanData],
)
def profit_plan(
    request: Request,
    payload: ProfitPlanRequest,
    _: None = Depends(require_profit_api_key),
) -> Dict[str, Any]:
    return _profit_response(
        request,
        payload,
        build_initial_profit_plan(payload),
        endpoint_semantics="initial_profit_plan",
    )


@app.post(
    "/profit/monitor",
    response_model=StandardAgentResponse[ProfitMonitorData],
)
def profit_monitor(
    request: Request,
    payload: ProfitPlanRequest,
    _: None = Depends(require_profit_api_key),
) -> Dict[str, Any]:
    return _profit_response(
        request,
        payload,
        build_profit_monitor(payload),
        endpoint_semantics="position_monitor",
    )


@app.post(
    "/profit/exit-signal",
    response_model=StandardAgentResponse[ProfitExitSignalData],
)
def profit_exit_signal(
    request: Request,
    payload: ProfitPlanRequest,
    _: None = Depends(require_profit_api_key),
) -> Dict[str, Any]:
    return _profit_response(
        request,
        payload,
        build_exit_signal(payload),
        endpoint_semantics="risk_gate_exit_signal",
    )


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {"message": "Profit Agent is running"}