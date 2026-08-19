"""Centralized IBKR placeOrder pacing. Shared by all baskets on one adapter."""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class OrderSubmitPacer:
    """Serialize broker submissions with a minimum interval (no burst loops)."""

    def __init__(self, min_interval_sec: float = 0.2) -> None:
        if min_interval_sec < 0:
            raise ValueError("min_interval_sec must be >= 0")
        self.min_interval_sec = min_interval_sec
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self) -> bool:
        """Wait until a submit slot is available. Returns True if delayed."""
        async with self._lock:
            now = time.monotonic()
            wait = self.min_interval_sec - (now - self._last)
            delayed = wait > 0
            if delayed:
                logger.info(
                    "IBKR submit paced: delay=%.3fs min_interval=%.3fs",
                    wait,
                    self.min_interval_sec,
                )
                await asyncio.sleep(wait)
            self._last = time.monotonic()
            return delayed
