from __future__ import annotations

import os


PROFIT_AGENT_VERSION = "0.2.0"
PROFIT_SCHEMA_VERSION = "profit-decision.v2"
LEGACY_SCHEMA_VERSION = "profit-plan.v1"
SUPPORTED_SCHEMA_VERSIONS = {
    LEGACY_SCHEMA_VERSION,
    PROFIT_SCHEMA_VERSION,
}


def app_environment() -> str:
    return os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "development")).strip().lower()


def profit_agent_api_key() -> str:
    return os.getenv("PROFIT_AGENT_API_KEY", "").strip()


def _positive_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


def _positive_float(name: str, default: float) -> float:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive number") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive number")
    return value


def max_request_body_bytes() -> int:
    return _positive_int("PROFIT_MAX_REQUEST_BODY_BYTES", 65536)


def request_timeout_seconds() -> float:
    return _positive_float("PROFIT_REQUEST_TIMEOUT_SECONDS", 30.0)


def rate_limit_per_minute() -> int:
    return _positive_int("PROFIT_RATE_LIMIT_PER_MINUTE", 120)


def profit_agent_host() -> str:
    return os.getenv("PROFIT_AGENT_HOST", "127.0.0.1").strip() or "127.0.0.1"


def profit_agent_port() -> int:
    return _positive_int("PROFIT_AGENT_PORT", 8011)


def profit_agent_workers() -> int:
    return _positive_int("PROFIT_AGENT_WORKERS", 1)


def profit_agent_log_level() -> str:
    value = os.getenv("PROFIT_AGENT_LOG_LEVEL", "INFO").strip().upper()
    if value not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
        raise RuntimeError("PROFIT_AGENT_LOG_LEVEL has an invalid value")
    return value


def market_data_max_age_seconds() -> int:
    return _positive_int("PROFIT_MARKET_DATA_MAX_AGE_SECONDS", 120)


def validate_runtime_configuration() -> None:
    if app_environment() in {"production", "prod"} and not profit_agent_api_key():
        raise RuntimeError("PROFIT_AGENT_API_KEY is required in production")
    max_request_body_bytes()
    request_timeout_seconds()
    rate_limit_per_minute()
    market_data_max_age_seconds()
    profit_agent_port()
    profit_agent_workers()
    profit_agent_log_level()