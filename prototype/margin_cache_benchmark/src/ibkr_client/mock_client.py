"""Mock IBKR client for local testing — no Gateway needed."""

from __future__ import annotations

import asyncio
import random
import time

from ..models import Instrument
from ..rate_limiter import PrototypeRateLimiter


class MockIBKRClient:
    """Simulates contract lookup + margin with configurable latency/failure.

    All benchmark logic can be verified without Gateway.
    """

    def __init__(
        self,
        rate_limiter: PrototypeRateLimiter | None = None,
        contract_latency_ms: tuple[float, float] = (20, 80),
        margin_latency_ms: tuple[float, float] = (30, 120),
        failure_rate: float = 0.02,
        pacing_error_rate: float = 0.01,
        timeout_rate: float = 0.005,
        seed: int = 42,
    ) -> None:
        self.rate_limiter = rate_limiter
        self.contract_latency_ms = contract_latency_ms
        self.margin_latency_ms = margin_latency_ms
        self.failure_rate = failure_rate
        self.pacing_error_rate = pacing_error_rate
        self.timeout_rate = timeout_rate
        self._rng = random.Random(seed)
        self._connected = False
        self.pacing_errors = 0
        self._con_id_counter = 100000

    async def connect(self) -> None:
        await asyncio.sleep(0.01)
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    async def resolve_contract(self, instrument: Instrument) -> tuple[int | None, float]:
        start = time.monotonic()
        if self.rate_limiter is not None:
            try:
                await self.rate_limiter.acquire()
            except Exception:
                self.pacing_errors += 1
                raise
        # Simulate pacing error occasionally
        if self._rng.random() < self.pacing_error_rate:
            self.pacing_errors += 1
            raise RuntimeError("IBKR Error 100: pacing violation (mock)")
        if self._rng.random() < self.timeout_rate:
            await asyncio.sleep(0.2)
            raise TimeoutError("ContractDetails timeout (mock)")

        delay = self._rng.uniform(*self.contract_latency_ms) / 1000.0
        await asyncio.sleep(delay)

        if self._rng.random() < self.failure_rate:
            raise RuntimeError(f"Mock contract lookup failure for {instrument.symbol}")

        # deterministic conId from symbol
        con_id = abs(hash(instrument.symbol)) % 900000 + 100000
        elapsed_ms = (time.monotonic() - start) * 1000
        return con_id, elapsed_ms

    async def fetch_margin(self, instrument: Instrument, con_id: int | None) -> tuple[str, str, float]:
        # DEFENSIVE: this is mock, but document that real impl must use whatIf
        # NEVER transmit executable order — enforced in real_client.py
        start = time.monotonic()
        if self.rate_limiter is not None:
            try:
                await self.rate_limiter.acquire()
            except Exception:
                self.pacing_errors += 1
                raise

        if self._rng.random() < self.pacing_error_rate:
            self.pacing_errors += 1
            raise RuntimeError("IBKR Error 100: pacing violation on margin (mock)")
        if self._rng.random() < self.timeout_rate:
            await asyncio.sleep(0.2)
            raise TimeoutError("Margin timeout (mock)")

        delay = self._rng.uniform(*self.margin_latency_ms) / 1000.0
        await asyncio.sleep(delay)

        if self._rng.random() < self.failure_rate:
            raise RuntimeError(f"Mock margin failure for {instrument.symbol}")

        # Simulate margin values
        init = f"{self._rng.uniform(500, 5000):.2f}"
        maint = f"{self._rng.uniform(400, 4000):.2f}"
        elapsed_ms = (time.monotonic() - start) * 1000
        return init, maint, elapsed_ms
