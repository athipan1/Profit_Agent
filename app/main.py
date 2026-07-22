from __future__ import annotations

import re
from typing import Any, Dict
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import validate_runtime_configuration
from app.models import HealthData, ProfitPlanData, ProfitPlanRequest
from app.security import (
    ProfitAuthenticationError,
    ProfitConfigurationError,
    require_profit_api_key,
)
from app.service import build_profit_plan
from app.system_contract import contract_response, router as system_contract_router


CORRELATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

validate_runtime_configuration()

app = FastAPI(
    title="Profit Agent",
    description="Profit-taking advisory service for the multi-agent trading system.",
    version="0.2.0",
)
app.include_router(system_contract_router)


def _correlation_id(request: Request) -> str:
    return str(request.state.correlation_id)


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    supplied = (request.headers.get("X-Correlation-ID") or "").strip()
    request.state.correlation_id = (
        supplied if CORRELATION_ID_PATTERN.fullmatch(supplied) else str(uuid4())
    )
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = request.state.correlation_id
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


def _profit_response(request: Request, payload: ProfitPlanRequest) -> Dict[str, Any]:
    data: ProfitPlanData = build_profit_plan(payload)
    confidence = max((item.confidence_score for item in data.actions), default=None)
    return contract_response(
        status="success",
        correlation_id=_correlation_id(request),
        data=data.model_dump(mode="json"),
        metadata={
            "advisory_only": True,
            "request_schema_version": payload.schema_version or "profit-plan.v1",
        },
        confidence_score=confidence,
    )


@app.post("/profit/plan")
def profit_plan(
    request: Request,
    payload: ProfitPlanRequest,
    _: None = Depends(require_profit_api_key),
) -> Dict[str, Any]:
    return _profit_response(request, payload)


@app.post("/profit/monitor")
def profit_monitor(
    request: Request,
    payload: ProfitPlanRequest,
    _: None = Depends(require_profit_api_key),
) -> Dict[str, Any]:
    return _profit_response(request, payload)


@app.post("/profit/exit-signal")
def profit_exit_signal(
    request: Request,
    payload: ProfitPlanRequest,
    _: None = Depends(require_profit_api_key),
) -> Dict[str, Any]:
    return _profit_response(request, payload)


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {"message": "Profit Agent is running"}
