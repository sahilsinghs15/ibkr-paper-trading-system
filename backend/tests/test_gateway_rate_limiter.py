"""Unit tests for GatewayRateLimiter."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.broker.ibkr.gateway_rate_limiter import (
    PRIORITY_EMERGENCY_FLATTEN,
    PRIORITY_MARKET_DATA,
    PRIORITY_ORDER_EXECUTION,
    GatewayPacingTimeout,
    GatewayRateLimiter,
)


def test_p0_proceeds_when_normal_bucket_empty() -> None:
    limiter = GatewayRateLimiter(
        max_msg_per_sec=10.0,
        normal_msg_per_sec=4.0,
        emergency_reserve_per_sec=6.0,
        max_wait_sec=2.0,
        error100_cooldown_sec=0.0,
    )
    for _ in range(4):
        assert limiter.try_acquire(PRIORITY_ORDER_EXECUTION, "placeOrder") is not None
    assert limiter.try_acquire(PRIORITY_ORDER_EXECUTION, "placeOrder") is None
    assert limiter.try_acquire(PRIORITY_EMERGENCY_FLATTEN, "placeOrder") is not None


@pytest.mark.asyncio
async def test_acquire_waits_outside_lock() -> None:
    limiter = GatewayRateLimiter(
        max_msg_per_sec=5.0,
        normal_msg_per_sec=5.0,
        emergency_reserve_per_sec=0.0,
        max_wait_sec=2.0,
        error100_cooldown_sec=0.0,
        max_burst=1.0,
    )
    assert limiter.try_acquire(PRIORITY_ORDER_EXECUTION, "a") is not None
    started = time.monotonic()

    async def second() -> None:
        await limiter.acquire(PRIORITY_ORDER_EXECUTION, "b")

    task = asyncio.create_task(second())
    await asyncio.sleep(0.05)
    await task
    elapsed = time.monotonic() - started
    assert elapsed >= 0.15


@pytest.mark.asyncio
async def test_acquire_timeout_raises() -> None:
    limiter = GatewayRateLimiter(
        max_msg_per_sec=1.0,
        normal_msg_per_sec=1.0,
        emergency_reserve_per_sec=0.0,
        max_wait_sec=0.05,
        error100_cooldown_sec=0.0,
    )
    assert limiter.try_acquire(PRIORITY_ORDER_EXECUTION, "first") is not None
    with pytest.raises(GatewayPacingTimeout):
        await limiter.acquire(PRIORITY_ORDER_EXECUTION, "second")


def test_notify_error100_blocks_acquire() -> None:
    limiter = GatewayRateLimiter(
        max_msg_per_sec=30.0,
        normal_msg_per_sec=24.0,
        emergency_reserve_per_sec=6.0,
        max_wait_sec=2.0,
        error100_cooldown_sec=0.2,
    )
    limiter.notify_error_100()
    assert limiter.try_acquire(PRIORITY_ORDER_EXECUTION, "placeOrder") is None
    time.sleep(0.25)
    assert limiter.try_acquire(PRIORITY_ORDER_EXECUTION, "placeOrder") is not None


def test_try_acquire_never_blocks() -> None:
    limiter = GatewayRateLimiter(
        max_msg_per_sec=1.0,
        normal_msg_per_sec=1.0,
        emergency_reserve_per_sec=0.0,
        max_wait_sec=2.0,
        error100_cooldown_sec=0.0,
    )
    assert limiter.try_acquire(PRIORITY_ORDER_EXECUTION, "first") is not None
    start = time.monotonic()
    assert limiter.try_acquire(PRIORITY_MARKET_DATA, "md") is None
    assert time.monotonic() - start < 0.05


def test_blocking_acquire_respects_timeout() -> None:
    limiter = GatewayRateLimiter(
        max_msg_per_sec=1.0,
        normal_msg_per_sec=1.0,
        emergency_reserve_per_sec=0.0,
        max_wait_sec=2.0,
        error100_cooldown_sec=0.0,
    )
    assert limiter.try_acquire(PRIORITY_ORDER_EXECUTION, "first") is not None
    assert (
        limiter.blocking_acquire(
            PRIORITY_ORDER_EXECUTION, "second", timeout=0.05
        )
        is None
    )
