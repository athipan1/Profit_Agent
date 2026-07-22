from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from typing import Any

from prometheus_client import Counter, Histogram


PROFIT_REQUESTS = Counter(
    "profit_requests_total",
    "Profit Agent HTTP requests",
    ("endpoint", "status"),
)
PROFIT_VALIDATION_FAILURES = Counter(
    "profit_validation_failures_total",
    "Rejected Profit request payloads",
)
PROFIT_STOP_BREACHES = Counter(
    "profit_stop_breaches_total",
    "Hard stop-loss breach advisories",
)
PROFIT_TRAILING_STOP_BREACHES = Counter(
    "profit_trailing_stop_breaches_total",
    "Trailing-stop breach advisories",
)
PROFIT_PARTIAL_EXIT_DECISIONS = Counter(
    "profit_partial_exit_decisions_total",
    "Partial-exit advisories",
)
PROFIT_DUPLICATE_DECISIONS = Counter(
    "profit_duplicate_decisions_total",
    "Lifecycle-suppressed duplicate target decisions",
)
PROFIT_PEAK_FALLBACKS = Counter(
    "profit_peak_fallback_total",
    "Requests using an inferred position peak",
)
PROFIT_REQUEST_DURATION = Histogram(
    "profit_request_duration_seconds",
    "Profit Agent HTTP request duration",
    ("endpoint",),
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "event": getattr(record, "event", "application_log"),
            "message": record.getMessage(),
        }
        fields = getattr(record, "safe_fields", {})
        if isinstance(fields, dict):
            payload.update(fields)
        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_json_logging(level: str) -> logging.Logger:
    logger = logging.getLogger("profit-agent")
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    return logger


LOGGER = configure_json_logging("INFO")


def log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    LOGGER.log(
        level,
        event,
        extra={"event": event, "safe_fields": fields},
    )