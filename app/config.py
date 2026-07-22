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


def validate_runtime_configuration() -> None:
    if app_environment() in {"production", "prod"} and not profit_agent_api_key():
        raise RuntimeError("PROFIT_AGENT_API_KEY is required in production")
