"""Offline unit tests for RatePacer token bucket rate limiter."""

import threading
import time

import pytest

from scripts.instrument_master.pacer import RatePacer


def test_pacer_initialization() -> None:
    """Test RatePacer parameter validation and initialization."""
    pacer = RatePacer(rate_limit_hz=10.0)
    assert pacer.rate_limit_hz == 10.0
    assert pacer.interval == 0.1

    with pytest.raises(ValueError):
        RatePacer(rate_limit_hz=0.0)

    with pytest.raises(ValueError):
        RatePacer(rate_limit_hz=-5.0)


def test_pacer_token_acquisition_pacing() -> None:
    """Test that RatePacer enforces time spacing between acquisitions."""
    # 20 req/sec = 0.05s per token
    pacer = RatePacer(rate_limit_hz=20.0, max_burst=1.0)

    start = time.monotonic()
    for _ in range(5):
        acquired = pacer.acquire()
        assert acquired

    elapsed = time.monotonic() - start
    # 5 acquisitions at 20 req/sec should take at least 4 intervals (~0.20 seconds)
    assert elapsed >= 0.15


def test_pacer_timeout() -> None:
    """Test timeout when acquire cannot get a token within timeout."""
    pacer = RatePacer(rate_limit_hz=1.0, max_burst=1.0)
    assert pacer.acquire()  # Consumes initial token

    start = time.monotonic()
    # Attempting acquire with short timeout should fail
    acquired = pacer.acquire(timeout=0.05)
    elapsed = time.monotonic() - start

    assert not acquired
    assert 0.04 <= elapsed <= 0.15


def test_pacer_multithreaded_safety() -> None:
    """Test thread-safe token acquisition across multiple concurrent threads."""
    pacer = RatePacer(rate_limit_hz=50.0, max_burst=1.0)
    count = 0
    lock = threading.Lock()

    def worker() -> None:
        nonlocal count
        for _ in range(5):
            if pacer.acquire():
                with lock:
                    count += 1

    threads = [threading.Thread(target=worker) for _ in range(4)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    elapsed = time.monotonic() - start
    assert count == 20
    assert elapsed >= 0.30
