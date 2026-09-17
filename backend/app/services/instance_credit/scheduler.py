"""Daily scheduler for instance credit AWS fetch.

Fetches previous UTC calendar day at ~08:00 UTC, when Cost Explorer
daily costs are reliably available. Cost Explorer data latency is typically
~24 hours; AWS states data is updated at least once daily and is usually
available within 24 hours after the day ends. Scheduling at 08:00 UTC
ensures Sep 14 actual is available when Sep 15 08:00 job runs, with
retry-if-unavailable handled by not persisting $0 and retaining estimate.

Normal operation: at most one AWS API call per day (EC2+IPv4 combined
requires 1-2 CE requests batched together, still counted as daily fetch).

Idempotent: ledger uniqueness prevents duplicate inserts.

Manual refresh is protected via POST /api/v1/system-monitor/credit/refresh
(admin only, separate from polling).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.services.instance_credit.service import InstanceCreditService

logger = logging.getLogger(__name__)


class InstanceCreditScheduler:
    """Background asyncio task that sleeps until next scheduled UTC time."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        enabled: bool | None = None,
        service: InstanceCreditService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._service = service or InstanceCreditService(session_factory)
        settings = get_settings()
        self._enabled = enabled if enabled is not None else settings.instance_credit_enabled
        self._hour = settings.instance_credit_fetch_hour_utc
        self._minute = settings.instance_credit_fetch_minute_utc
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def _next_run(self, now: datetime | None = None) -> datetime:
        now = now or datetime.now(UTC)
        target = now.replace(hour=self._hour, minute=self._minute, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)
        return target

    async def _run_once_for_previous_day(self) -> None:
        # Target is previous UTC calendar day
        target_date = (datetime.now(UTC) - timedelta(days=1)).date()
        try:
            res = await self._service.fetch_and_persist_for_date(target_date)
            if res is None:
                logger.warning("InstanceCreditScheduler: no data persisted for %s (will retry tomorrow)", target_date)
            else:
                logger.info("InstanceCreditScheduler: fetched %s total=%s", target_date, res.total_cost_usd)
        except Exception:
            logger.exception("InstanceCreditScheduler fetch failed for %s", target_date)

    async def _loop(self) -> None:
        logger.info(
            "InstanceCreditScheduler started: daily at %02d:%02d UTC, latency-aware (~24h CE delay)",
            self._hour,
            self._minute,
        )
        # On startup, optionally catch up if yesterday missing and data should be available
        # Only attempt catch-up if after scheduled time today
        try:
            now = datetime.now(UTC)
            if now.hour > self._hour or (now.hour == self._hour and now.minute >= self._minute + 5):
                await self._run_once_for_previous_day()
        except Exception:
            logger.exception("InstanceCreditScheduler startup catch-up failed")

        while not self._stop.is_set():
            nxt = self._next_run()
            sleep_sec = (nxt - datetime.now(UTC)).total_seconds()
            logger.info("InstanceCreditScheduler next run at %s (in %.0fs)", nxt.isoformat(), sleep_sec)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_sec)
                break
            except TimeoutError:
                pass
            await self._run_once_for_previous_day()

    async def start(self) -> None:
        if not self._enabled:
            logger.info("InstanceCreditScheduler disabled via config")
            return
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            self._task = None
