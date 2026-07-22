from __future__ import annotations

from threading import Lock
import time


class FixedWindowRateLimiter:
    """Small per-process limiter for authenticated service traffic."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._windows: dict[str, tuple[int, int]] = {}

    def allow(self, key: str, limit: int, *, now: float | None = None) -> bool:
        minute = int((time.time() if now is None else now) // 60)
        with self._lock:
            window, count = self._windows.get(key, (minute, 0))
            if window != minute:
                window, count = minute, 0
            if count >= limit:
                self._windows[key] = (window, count)
                return False
            self._windows[key] = (window, count + 1)
            return True

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()


profit_rate_limiter = FixedWindowRateLimiter()