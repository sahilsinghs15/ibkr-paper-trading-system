"""Per-gateway IBKR API rate limiter (token bucket, priority, Error 100 cooldown)."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Documented IBKR Error 100 limit: 50 msg/sec
IBKR_DOCUMENTED_CEILING_MSG_PER_SEC = 50.0

DEFAULT_MAX_MSG_PER_SEC = 30.0
DEFAULT_NORMAL_MSG_PER_SEC = 24.0
DEFAULT_EMERGENCY_RESERVE_PER_SEC = 6.0
DEFAULT_MAX_WAIT_SEC = 8.0
DEFAULT_ERROR100_COOLDOWN_SEC = 2.0

PRIORITY_EMERGENCY_FLATTEN = 0
PRIORITY_ORDER_EXECUTION = 1
PRIORITY_CONTRACT_DETAILS = 2
PRIORITY_MARKET_DATA = 3
PRIORITY_DIAGNOSTIC = 4


class GatewayPacingTimeout(Exception):
    """Raised when acquire() exceeds max_wait_sec without granting a token."""


@dataclass(frozen=True)
class AcquireResult:
    """Outcome of a successful rate-limiter acquire."""

    delayed: bool
    priority: int
    request_type: str
    waited_sec: float = 0.0


class GatewayRateLimiter:
    """In-process token bucket for one IB Gateway connection.

    P0 (EMERGENCY_FLATTEN) spends global tokens only and may use the emergency
    reserve slice when the normal bucket is empty. P1–P4 require both a global
    and a normal token. Sleep happens outside the lock.
    """

    def __init__(
        self,
        *,
        max_msg_per_sec: float = DEFAULT_MAX_MSG_PER_SEC,
        normal_msg_per_sec: float = DEFAULT_NORMAL_MSG_PER_SEC,
        emergency_reserve_per_sec: float = DEFAULT_EMERGENCY_RESERVE_PER_SEC,
        max_wait_sec: float = DEFAULT_MAX_WAIT_SEC,
        error100_cooldown_sec: float = DEFAULT_ERROR100_COOLDOWN_SEC,
        gateway_id: str = "default",
        max_burst: float | None = None,
    ) -> None:
        if max_msg_per_sec <= 0:
            raise ValueError("max_msg_per_sec must be positive")
        if normal_msg_per_sec <= 0:
            raise ValueError("normal_msg_per_sec must be positive")
        if normal_msg_per_sec > max_msg_per_sec:
            raise ValueError("normal_msg_per_sec must be <= max_msg_per_sec")
        if emergency_reserve_per_sec < 0:
            raise ValueError("emergency_reserve_per_sec must be >= 0")
        if max_wait_sec <= 0:
            raise ValueError("max_wait_sec must be positive")
        if error100_cooldown_sec < 0:
            raise ValueError("error100_cooldown_sec must be >= 0")

        self.max_msg_per_sec = max_msg_per_sec
        self.normal_msg_per_sec = normal_msg_per_sec
        self.emergency_reserve_per_sec = emergency_reserve_per_sec
        self.max_wait_sec = max_wait_sec
        self.error100_cooldown_sec = error100_cooldown_sec
        self.gateway_id = gateway_id
        burst_cap = max(1.0, max_burst if max_burst is not None else max_msg_per_sec)

        self._lock = threading.Lock()
        self._global_burst_cap = burst_cap
        self._normal_burst_cap = min(burst_cap, normal_msg_per_sec)
        self._global_tokens = float(burst_cap)
        self._normal_tokens = float(self._normal_burst_cap)
        self._last_refill = time.monotonic()
        self._cooldown_until = 0.0

        self.metrics: dict[str, Any] = {
            "total_acquired": 0,
            "delayed_count": 0,
            "timeout_count": 0,
            "try_acquire_denied": 0,
            "error100_cooldowns": 0,
            "requests_by_priority": {p: 0 for p in range(5)},
            "requests_by_type": {},
        }

    def notify_error_100(self) -> None:
        """Apply IB Error 100 backoff: drain buckets and pause grants."""
        now = time.monotonic()
        with self._lock:
            self._global_tokens = 0.0
            self._normal_tokens = 0.0
            self._cooldown_until = now + self.error100_cooldown_sec
            self._last_refill = now
            self.metrics["error100_cooldowns"] += 1
        logger.warning(
            "IBKR Error 100 cooldown: gateway_id=%s cooldown_sec=%.2f",
            self.gateway_id,
            self.error100_cooldown_sec,
        )

    def try_acquire(
        self,
        priority: int,
        request_type: str = "general",
    ) -> AcquireResult | None:
        """Non-blocking acquire. Returns None if no token is available now."""
        delayed = False
        with self._lock:
            if not self._try_consume_locked(priority):
                self.metrics["try_acquire_denied"] += 1
                return None
            self._record_acquire_locked(priority, request_type, delayed=False)
        return AcquireResult(
            delayed=delayed,
            priority=priority,
            request_type=request_type,
            waited_sec=0.0,
        )

    async def acquire(
        self,
        priority: int,
        request_type: str = "general",
    ) -> AcquireResult:
        """Wait up to max_wait_sec for a token. Raises GatewayPacingTimeout on expiry."""
        deadline = time.monotonic() + self.max_wait_sec
        total_waited = 0.0
        delayed = False

        while True:
            wait_sec = 0.0
            with self._lock:
                if self._try_consume_locked(priority):
                    self._record_acquire_locked(priority, request_type, delayed=delayed)
                    if delayed:
                        logger.info(
                            "IBKR submit paced: gateway_id=%s priority=%d type=%s "
                            "waited_sec=%.3f",
                            self.gateway_id,
                            priority,
                            request_type,
                            total_waited,
                        )
                    return AcquireResult(
                        delayed=delayed,
                        priority=priority,
                        request_type=request_type,
                        waited_sec=total_waited,
                    )
                wait_sec = self._seconds_until_available_locked(priority)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                with self._lock:
                    self.metrics["timeout_count"] += 1
                logger.warning(
                    "Gateway pacing timeout: gateway_id=%s priority=%d type=%s "
                    "max_wait_sec=%.2f waited_sec=%.3f",
                    self.gateway_id,
                    priority,
                    request_type,
                    self.max_wait_sec,
                    total_waited,
                )
                raise GatewayPacingTimeout(
                    f"Gateway pacing timeout after {self.max_wait_sec:.1f}s "
                    f"(priority={priority}, type={request_type})"
                )

            sleep_for = min(wait_sec, remaining, 0.05)
            if sleep_for > 0:
                delayed = True
                await asyncio.sleep(sleep_for)
                total_waited += sleep_for

    def blocking_acquire(
        self,
        priority: int,
        request_type: str = "general",
        *,
        timeout: float | None = None,
    ) -> AcquireResult | None:
        """Synchronous acquire for TWS callback / worker threads. Returns None on timeout."""
        max_wait = timeout if timeout is not None else self.max_wait_sec
        deadline = time.monotonic() + max_wait
        total_waited = 0.0
        delayed = False

        while True:
            wait_sec = 0.0
            with self._lock:
                if self._try_consume_locked(priority):
                    self._record_acquire_locked(priority, request_type, delayed=delayed)
                    return AcquireResult(
                        delayed=delayed,
                        priority=priority,
                        request_type=request_type,
                        waited_sec=total_waited,
                    )
                wait_sec = self._seconds_until_available_locked(priority)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                with self._lock:
                    self.metrics["timeout_count"] += 1
                return None

            sleep_for = min(wait_sec, remaining, 0.05)
            if sleep_for > 0:
                delayed = True
                time.sleep(sleep_for)
                total_waited += sleep_for

    def _record_acquire_locked(self, priority: int, request_type: str, *, delayed: bool) -> None:
        self.metrics["total_acquired"] += 1
        if delayed:
            self.metrics["delayed_count"] += 1
        clamped = max(0, min(4, priority))
        self.metrics["requests_by_priority"][clamped] = (
            self.metrics["requests_by_priority"].get(clamped, 0) + 1
        )
        self.metrics["requests_by_type"][request_type] = (
            self.metrics["requests_by_type"].get(request_type, 0) + 1
        )

    def _refill_locked(self, now: float) -> None:
        elapsed = max(0.0, now - self._last_refill)
        if elapsed <= 0:
            return
        self._last_refill = now
        self._global_tokens = min(
            self._global_burst_cap,
            self._global_tokens + elapsed * self.max_msg_per_sec,
        )
        self._normal_tokens = min(
            self._normal_burst_cap,
            self._normal_tokens + elapsed * self.normal_msg_per_sec,
        )

    def _in_cooldown_locked(self, now: float) -> bool:
        return now < self._cooldown_until

    def _try_consume_locked(self, priority: int) -> bool:
        now = time.monotonic()
        if self._in_cooldown_locked(now):
            return False
        self._refill_locked(now)
        if self._global_tokens < 1.0:
            return False
        is_emergency = priority == PRIORITY_EMERGENCY_FLATTEN
        if is_emergency:
            self._global_tokens -= 1.0
            return True
        if self._normal_tokens < 1.0:
            return False
        self._global_tokens -= 1.0
        self._normal_tokens -= 1.0
        return True

    def _seconds_until_available_locked(self, priority: int) -> float:
        now = time.monotonic()
        if self._in_cooldown_locked(now):
            return max(0.001, self._cooldown_until - now)
        self._refill_locked(now)
        if self._global_tokens >= 1.0:
            if priority == PRIORITY_EMERGENCY_FLATTEN:
                return 0.0
            if self._normal_tokens >= 1.0:
                return 0.0
            needed = 1.0 - self._normal_tokens
            return max(0.001, needed / self.normal_msg_per_sec)
        needed_global = 1.0 - self._global_tokens
        return max(0.001, needed_global / self.max_msg_per_sec)
