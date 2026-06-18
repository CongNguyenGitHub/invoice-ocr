"""Token bucket rate limiter — per-process, asyncio.Lock protected.

Refreshed from Redis every RATE_LIMIT_REFRESH_INTERVAL seconds (handled by
worker/rate_refresh daemon). `acquire()` is non-blocking: returns True if a
token was consumed, False otherwise — caller's responsibility to raise
RateLimitedLocallyError and yield.
"""

from __future__ import annotations

import asyncio
import time


class TokenBucket:
    def __init__(self, rate_per_second: float, burst: int) -> None:
        self._rate = rate_per_second
        self._capacity = burst
        self._tokens: float = float(burst)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._last = now
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    async def update_config(self, rate_per_second: float, burst: int) -> None:
        async with self._lock:
            self._rate = rate_per_second
            self._capacity = burst
            self._tokens = min(self._tokens, float(burst))

    @property
    def available(self) -> float:
        return self._tokens
