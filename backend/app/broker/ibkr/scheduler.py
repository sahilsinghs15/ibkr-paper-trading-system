"""Centralized IBKR execution scheduler with token bucket rate limiting and priority concurrency gating."""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class IBKRExecutionScheduler:
    """Bounded concurrency and leaky/token-bucket rate limiter for IBKR TWS API operations."""

    def __init__(
        self,
        *,
        max_rate_per_sec: float = 40.0,
        max_concurrent: int = 10,
    ) -> None:
        self._max_rate = max_rate_per_sec
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._tokens = max_rate_per_sec
        self._last_fill = time.monotonic()
        self._lock = asyncio.Lock()

    async def _acquire_token(self) -> None:
        """Acquire a rate-limit token according to token bucket algorithm."""
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._last_fill
                self._tokens = min(self._max_rate, self._tokens + elapsed * self._max_rate)
                self._last_fill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait_time = max(0.005, (1.0 - self._tokens) / self._max_rate)
                await asyncio.sleep(wait_time)

    async def execute_paced(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Execute a broker action (e.g. placeOrder, cancelOrder) subject to pacing and concurrency limits."""
        await self._acquire_token()
        async with self._semaphore:
            if asyncio.iscoroutinefunction(func):
                return await func(*args, **kwargs)
            return await asyncio.to_thread(func, *args, **kwargs)
