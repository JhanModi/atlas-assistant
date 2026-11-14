"""A small thread-safe token bucket, shared by the model rate-limit hook and (Phase 2+) connectors."""

from __future__ import annotations

import threading
import time
from typing import Callable


class TokenBucket:
    def __init__(
        self,
        rate_per_minute: float,
        burst: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be > 0 (use no limiter for unlimited)")
        self.rate = rate_per_minute / 60.0
        self.capacity = float(burst if burst is not None else max(1, int(rate_per_minute // 6) or 1))
        self.tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
        self._last = now

    def acquire(self) -> float:
        """Block until a token is available. Returns seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                self._refill()
                if self.tokens >= 1:
                    self.tokens -= 1
                    return waited
                need = (1 - self.tokens) / self.rate
            self._sleep(need)
            waited += need
