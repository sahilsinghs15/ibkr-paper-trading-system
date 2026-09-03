"""Isolated token-bucket rate limiter for prototype — independent from production GatewayRateLimiter."""

from __future__ import annotations

import asyncio
import time
import threading
from dataclasses import dataclass


class RateLimitTimeout(Exception):
    """Raised when acquire exceeds max_wait."""


@dataclass
class RateLimitMetrics:
    total_acquired: int = 0
    delayed_count: int = 0
    timeout_count: int = 0
    pacing_errors: int = 0


class PrototypeRateLimiter:
    """Token-bucket limiter with configurable rate. Thread + async safe.

    Not imported from app.broker.ibkr.gateway_rate_limiter — isolated copy
    for prototype safety (task requirement: do not modify existing limiter).
    """

    def __init__(
        self,
        rate_per_sec: float = 10.0,
        max_wait_sec: float = 8.0,
        burst: float | None = None,
    ) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be > 0")
        if max_wait_sec <= 0:
            raise ValueError("max_wait_sec must be > 0")
        self.rate_per_sec = rate_per_sec
        self.max_wait_sec = max_wait_sec
        self.burst = burst if burst is not None else rate_per_sec
        self._tokens = float(self.burst)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()
        self.metrics = RateLimitMetrics()

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._last_refill)
        if elapsed > 0:
            self._last_refill = now
            self._tokens = min(self.burst, self._tokens + elapsed * self.rate_per_sec)

    def _try_consume(self) -> bool:
        now = time.monotonic()
        self._refill(now)
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    def _seconds_until_token(self) -> float:
        now = time.monotonic()
        self._refill(now)
        if self._tokens >= 1.0:
            return 0.0
        needed = 1.0 - self._tokens
        return max(0.001, needed / self.rate_per_sec)

    # --- sync blocking ---
    def blocking_acquire(self, timeout: float | None = None) -> bool:
        """Blocking acquire for sync benchmark. Returns True if acquired, False on timeout."""
        max_wait = timeout if timeout is not None else self.max_wait_sec
        deadline = time.monotonic() + max_wait
        delayed = False
        while True:
            with self._lock:
                if self._try_consume():
                    self.metrics.total_acquired += 1
                    if delayed:
                        self.metrics.delayed_count += 1
                    return True
                wait = self._seconds_until_token()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                with self._lock:
                    self.metrics.timeout_count += 1
                    self.metrics.pacing_errors += 1
                return False
            sleep_for = min(wait, remaining, 0.05)
            if sleep_for > 0:
                delayed = True
                time.sleep(sleep_for)

    # --- async ---
    async def acquire(self, timeout: float | None = None) -> bool:
        max_wait = timeout if timeout is not None else self.max_wait_sec
        deadline = time.monotonic() + max_wait
        delayed = False
        while True:
            with self._lock:
                if self._try_consume():
                    self.metrics.total_acquired += 1
                    if delayed:
                        self.metrics.delayed_count += 1
                    return True
                wait = self._seconds_until_token()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                with self._lock:
                    self.metrics.timeout_count += 1
                    self.metrics.pacing_errors += 1
                raise RateLimitTimeout(f"Rate limiter timeout after {max_wait:.1f}s")
            sleep_for = min(wait, remaining, 0.05)
            if sleep_for > 0:
                delayed = True
                await asyncio.sleep(sleep_for)
