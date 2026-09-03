"""Dedicated margin-cache worker pool — isolated from ExecutionWorkerPool."""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from .models import Instrument, MarginResult


class MarginCacheWorkerPool:
    """Small pool that fetches margins concurrently through rate limiter.

    Architecture:
        CSV queue -> MarginCacheWorkerPool (N workers) -> RateLimiter -> IB Gateway

    Independent from app.services.worker_pool.ExecutionWorkerPool.
    Does NOT process real TradingView signals.
    """

    def __init__(
        self,
        worker_count: int = 2,
        rate_limiter: object | None = None,
    ) -> None:
        if worker_count < 1:
            raise ValueError("worker_count must be >= 1")
        self.worker_count = worker_count
        self.rate_limiter = rate_limiter

    async def run(
        self,
        instruments: list[Instrument],
        fetch_one: Callable[[Instrument], Awaitable[MarginResult]],
    ) -> list[MarginResult]:
        """Run concurrent fetching with controlled concurrency."""
        queue: asyncio.Queue[Instrument | None] = asyncio.Queue()
        for inst in instruments:
            await queue.put(inst)
        # sentinel per worker
        for _ in range(self.worker_count):
            await queue.put(None)

        results: list[MarginResult] = []
        results_lock = asyncio.Lock()

        async def worker(worker_id: int) -> None:
            while True:
                item = await queue.get()
                if item is None:
                    queue.task_done()
                    break
                try:
                    res = await fetch_one(item)
                except Exception as exc:
                    from .models import utc_now_iso

                    res = MarginResult(
                        instrument_type=item.instrument_type,
                        symbol=item.symbol,
                        exchange=item.exchange,
                        currency=item.currency,
                        status="failed",
                        error=str(exc)[:500],
                        timestamp_utc=utc_now_iso(),
                    )
                async with results_lock:
                    results.append(res)
                queue.task_done()

        tasks = [asyncio.create_task(worker(i)) for i in range(self.worker_count)]
        await queue.join()
        await asyncio.gather(*tasks)
        # preserve original order by mapping back
        # results currently in completion order — keep that for realistic latency analysis,
        # but also return stable-ordered copy if needed
        return results
