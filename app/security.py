from __future__ import annotations

import hmac
from typing import Annotated, Optional

from fastapi import Security
from fastapi.security import APIKeyHeader

from app.config import profit_agent_api_key


class ProfitAuthenticationError(RuntimeError):
    pass


class ProfitConfigurationError(RuntimeError):
    pass


api_key_header = APIKeyHeader(name="X-API-KEY", auto_error=False)


def require_profit_api_key(
    x_api_key: Annotated[Optional[str], Security(api_key_header)] = None,
) -> None:
    expected = profit_agent_api_key()
    if not expected:
        raise ProfitConfigurationError("Profit Agent authentication is not configured")
    supplied = x_api_key or ""
    if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        raise ProfitAuthenticationError("Invalid or missing API key")
