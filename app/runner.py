from __future__ import annotations

import uvicorn

from app.config import (
    profit_agent_host,
    profit_agent_log_level,
    profit_agent_port,
    profit_agent_workers,
    validate_runtime_configuration,
)


def main() -> None:
    validate_runtime_configuration()
    uvicorn.run(
        "app.main:app",
        host=profit_agent_host(),
        port=profit_agent_port(),
        workers=profit_agent_workers(),
        log_level=profit_agent_log_level().lower(),
        access_log=False,
        server_header=False,
        timeout_graceful_shutdown=30,
    )


if __name__ == "__main__":
    main()