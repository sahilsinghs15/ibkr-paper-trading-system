"""Thread-safe Rate Pacer (Token Bucket) for IBKR API request rate limiting."""

import logging
import threading
import time

logger = logging.getLogger("pacer")


class RatePacer:
    """Thread-safe Token Bucket Rate Limiter.

    Ensures requests do not exceed the configured rate (requests per second)
    and prevents aggressive request bursts.
    """

    def __init__(self, rate_limit_hz: float = 20.0, max_burst: float = 1.0) -> None:
        """Initialize RatePacer.

        Args:
            rate_limit_hz: Maximum requests permitted per second (default: 20.0).
            max_burst: Maximum token capacity burst limit (default: 1.0).
        """
        if rate_limit_hz <= 0:
            raise ValueError("rate_limit_hz must be positive")

        self.rate_limit_hz = rate_limit_hz
        self.interval = 1.0 / rate_limit_hz
        self.max_tokens = max(1.0, max_burst)

        self._tokens = float(self.max_tokens)
        self._last_update = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, timeout: float | None = None) -> bool:
        """Acquire a token, blocking if necessary until one becomes available.

        Args:
            timeout: Maximum seconds to wait for a token (None for blocking).

        Returns:
            True if a token was acquired, False if timed out.
        """
        start_time = time.monotonic()

        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_update
                self._last_update = now

                # Add new tokens based on elapsed time
                self._tokens = min(
                    self.max_tokens, self._tokens + elapsed * self.rate_limit_hz
                )

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True

                # Calculate remaining wait time for next token
                needed_tokens = 1.0 - self._tokens
                wait_time = needed_tokens / self.rate_limit_hz

            if timeout is not None:
                elapsed_total = time.monotonic() - start_time
                if elapsed_total + wait_time > timeout:
                    # Sleep for remaining timeout if wait exceeds timeout
                    remaining = timeout - elapsed_total
                    if remaining > 0:
                        time.sleep(remaining)
                    return False

            time.sleep(max(0.001, wait_time))

    def reset(self) -> None:
        """Reset the token bucket state."""
        with self._lock:
            self._tokens = float(self.max_tokens)
            self._last_update = time.monotonic()
