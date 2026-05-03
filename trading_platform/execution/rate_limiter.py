from __future__ import annotations

import asyncio
import time


class TokenBucketRateLimiter:
    """Async token-bucket rate limiter.

    Angel One SmartAPI enforces ~3 orders/second and ~200 orders/day.
    Default: 3 tokens/second, burst of 5.
    """

    def __init__(self, rate: float = 3.0, burst: int = 5) -> None:
        self.rate = rate
        self.burst = burst
        self._tokens: float = float(burst)
        self._last_refill: float = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
                self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
                await asyncio.sleep(wait)

    @property
    def available_tokens(self) -> float:
        elapsed = time.monotonic() - self._last_refill
        return min(self.burst, self._tokens + elapsed * self.rate)
