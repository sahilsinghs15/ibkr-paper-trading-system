"""Centralized IBKR execution scheduler with token bucket rate limiting, priority scheduling, and reserved emergency capacity."""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# Documented IBKR Error 100 limit: 50 msg/sec
IBKR_DOCUMENTED_CEILING_MSG_PER_SEC = 50.0

# Application Safety Envelope
DEFAULT_GLOBAL_APP_BUDGET = 30.0  # msg/sec
DEFAULT_NORMAL_WORKLOAD_BUDGET = 24.0  # msg/sec
DEFAULT_EMERGENCY_RESERVE_BUDGET = 6.0  # msg/sec

# Priority Levels
PRIORITY_EMERGENCY_FLATTEN = 0
PRIORITY_ORDER_EXECUTION = 1
PRIORITY_CONTRACT_DETAILS = 2
PRIORITY_MARKET_DATA = 3
PRIORITY_DIAGNOSTIC = 4


class IBKRExecutionScheduler:
    """Centralized gatekeeper managing outbound IBKR API rate limits, priority queues, and emergency reserve capacity."""

    def __init__(
        self,
        *,
        max_rate_per_sec: float = DEFAULT_GLOBAL_APP_BUDGET,
        normal_rate_limit: float = DEFAULT_NORMAL_WORKLOAD_BUDGET,
        emergency_reserve: float = DEFAULT_EMERGENCY_RESERVE_BUDGET,
        max_concurrent: int = 10,
    ) -> None:
        self._global_max_rate = max_rate_per_sec
        self._normal_max_rate = normal_rate_limit
        self._emergency_reserve = emergency_reserve

        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._lock = asyncio.Lock()

        # Token buckets: Global and Normal Workload
        self._global_tokens = max_rate_per_sec
        self._normal_tokens = normal_rate_limit
        self._last_fill = time.monotonic()

        # Sequential Locks for Priority Queueing
        self._priority_locks: dict[int, asyncio.Lock] = {p: asyncio.Lock() for p in range(5)}

        # Observability Metrics
        self.metrics: dict[str, Any] = {
            "total_requests": 0,
            "requests_by_priority": {p: 0 for p in range(5)},
            "requests_by_type": {},
            "throttled_count": 0,
            "pacing_violations": 0,
            "max_observed_concurrency": 0,
            "current_concurrent": 0,
        }

    async def _acquire_token(self, priority: int) -> None:
        """Acquire a token from the token bucket, enforcing global and priority budget limits."""
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._last_fill
                self._last_fill = now

                # Replenish token buckets
                self._global_tokens = min(
                    self._global_max_rate,
                    self._global_tokens + elapsed * self._global_max_rate,
                )
                self._normal_tokens = min(
                    self._normal_max_rate,
                    self._normal_tokens + elapsed * self._normal_max_rate,
                )

                is_emergency = priority == PRIORITY_EMERGENCY_FLATTEN

                # Emergency CLOSE (P0) uses global budget; Normal (P1-P4) requires normal budget
                if self._global_tokens >= 1.0 and (is_emergency or self._normal_tokens >= 1.0):
                    self._global_tokens -= 1.0
                    if not is_emergency:
                        self._normal_tokens -= 1.0
                    return

                self.metrics["throttled_count"] += 1
                wait_rate = self._global_max_rate if is_emergency else self._normal_max_rate
                wait_time = max(0.005, 1.0 / wait_rate)
                await asyncio.sleep(wait_time)

    async def execute_paced(
        self,
        func: Callable[..., Any],
        *args: Any,
        priority: int = PRIORITY_ORDER_EXECUTION,
        request_type: str = "general",
        **kwargs: Any,
    ) -> Any:
        """Execute an outbound IBKR API function subject to single-gate priority pacing and concurrency limits."""
        priority_clamp = max(0, min(4, priority))

        async with self._priority_locks[priority_clamp]:
            await self._acquire_token(priority_clamp)
            async with self._semaphore:
                self.metrics["total_requests"] += 1
                self.metrics["requests_by_priority"][priority_clamp] += 1
                self.metrics["requests_by_type"][request_type] = (
                    self.metrics["requests_by_type"].get(request_type, 0) + 1
                )
                self.metrics["current_concurrent"] += 1
                self.metrics["max_observed_concurrency"] = max(
                    self.metrics["max_observed_concurrency"],
                    self.metrics["current_concurrent"],
                )
                try:
                    if asyncio.iscoroutinefunction(func):
                        return await func(*args, **kwargs)
                    return await asyncio.to_thread(func, *args, **kwargs)
                finally:
                    self.metrics["current_concurrent"] = max(
                        0, self.metrics["current_concurrent"] - 1
                    )
