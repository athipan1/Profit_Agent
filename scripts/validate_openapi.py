from __future__ import annotations

import sys
from pathlib import Path

from openapi_spec_validator import validate_spec

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import app  # noqa: E402


def main() -> None:
    schema = app.openapi()
    validate_spec(schema)
    required_paths = {
        "/health",
        "/ready",
        "/version",
        "/profit/plan",
        "/profit/monitor",
        "/profit/exit-signal",
    }
    missing = required_paths.difference(schema.get("paths", {}))
    if missing:
        raise RuntimeError(
            f"OpenAPI schema is missing required paths: {sorted(missing)}"
        )


if __name__ == "__main__":
    main()