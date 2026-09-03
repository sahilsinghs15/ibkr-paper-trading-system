import asyncio
import time
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest
from src.rate_limiter import PrototypeRateLimiter, RateLimitTimeout


def test_rate_limiter_burst():
    limiter = PrototypeRateLimiter(rate_per_sec=10, burst=10)
    # burst allows 10 immediate
    for _ in range(10):
        assert limiter.blocking_acquire(timeout=0.1) is True
    # 11th should timeout quickly
    assert limiter.blocking_acquire(timeout=0.05) is False
    assert limiter.metrics.pacing_errors >= 1

def test_blocking_rate_enforced():
    limiter = PrototypeRateLimiter(rate_per_sec=5, burst=5)
    start = time.monotonic()
    for _ in range(10):
        limiter.blocking_acquire(timeout=2)
    elapsed = time.monotonic() - start
    # 10 tokens at 5/sec with burst 5 -> ~1 sec for second 5
    assert elapsed >= 0.8

@pytest.mark.asyncio
async def test_async_acquire():
    limiter = PrototypeRateLimiter(rate_per_sec=10, burst=2)
    assert await limiter.acquire() is True
    assert await limiter.acquire() is True
    # third should delay
    t0 = time.monotonic()
    await limiter.acquire()
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.05

def test_configurable_rate():
    limiter = PrototypeRateLimiter(rate_per_sec=2)
    assert limiter.rate_per_sec == 2
