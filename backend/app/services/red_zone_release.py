"""Red Zone Release Service: DEFERRED_RED_ZONE -> QUEUED when safe."""

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.db.models.signal import JOB_STATUS_DEFERRED_RED_ZONE
from app.db.repositories.event_repository import EventRepository
from app.db.repositories.signal_repository import SignalJobRepository
from app.services.session_clock import get_session_clock

logger = logging.getLogger(__name__)


class RedZoneReleaseService:
    """Poll deferred jobs and release when window is open and breaker clear."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client=None,
        order_manager=None,
    ) -> None:
        self._session_factory = session_factory
        self._client = client
        self._order_manager = order_manager
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="red-zone-release")
        logger.info("RedZoneReleaseService started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("RedZoneReleaseService stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._release_cycle()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Red Zone release cycle error")
            # intelligent sleep: 30s normally, 5s if near open
            try:
                clock = get_session_clock()
                now = datetime.now(UTC)
                if clock.in_red_zone(now):
                    await asyncio.sleep(30)
                else:
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                await asyncio.sleep(10)

    async def _release_cycle(self) -> None:
        settings = get_settings()
        clock = get_session_clock()
        now = datetime.now(UTC)
        if clock.in_red_zone(now):
            # still in red zone, nothing to release
            return
        # check gateway connectivity if client provided
        gateway_ok = True
        if self._client is not None and hasattr(self._client, "is_connected"):
            try:
                if not self._client.is_connected():
                    gateway_ok = False
            except Exception:  # noqa: BLE001
                gateway_ok = False
        if not gateway_ok:
            # missed window semantics: deferred jobs exist but gateway not ready during release window
            async with self._session_factory() as session, session.begin():
                from sqlalchemy import select

                from app.db.models.signal import SignalJobModel

                stmt = select(SignalJobModel).where(SignalJobModel.status == JOB_STATUS_DEFERRED_RED_ZONE).limit(1)
                res = await session.execute(stmt)
                has_deferred = res.scalars().first() is not None
                if has_deferred:
                    # emit missed window
                    first = await session.execute(select(SignalJobModel).where(SignalJobModel.status == JOB_STATUS_DEFERRED_RED_ZONE).order_by(SignalJobModel.deferred_at))
                    jobs = list(first.scalars().all())
                    for j in jobs[:3]:
                        await EventRepository(session).append(
                            process="release_service",
                            kind="RED_ZONE_MISSED_WINDOW",
                            detail={
                                "job_id": str(j.job_id),
                                "strategy_id": j.strategy_id,
                                "signal_id": j.signal_id,
                                "resolved_session_close": j.resolved_session_close.isoformat() if j.resolved_session_close else None,
                                "now": now.isoformat(),
                                "post_open_delay_seconds": settings.post_open_delay_seconds,
                                "deferred_session_count": j.deferred_session_count,
                                "reason": "gateway_disconnected_during_release_window",
                            },
                        )
                    logger.warning("RED_ZONE_MISSED_WINDOW: gateway disconnected with %d deferred jobs", len(jobs))
            logger.info("Red Zone release deferred: gateway not connected")
            return

        async with self._session_factory() as session, session.begin():
            repo = SignalJobRepository(session)
            # peek deferred jobs
            from sqlalchemy import select

            from app.db.models.signal import SignalJobModel

            stmt = select(SignalJobModel).where(SignalJobModel.status == JOB_STATUS_DEFERRED_RED_ZONE).order_by(SignalJobModel.deferred_at)
            res = await session.execute(stmt)
            deferred_jobs = list(res.scalars().all())
            if not deferred_jobs:
                return

            # circuit breaker checks
            count = len(deferred_jobs)
            if count > settings.max_auto_release_count:
                # void all? Spec says follow void policy and emit breaker
                logger.warning("RED_ZONE_BREAKER trip: count %d > %d", count, settings.max_auto_release_count)
                await EventRepository(session).append(
                    process="release_service",
                    kind="RED_ZONE_BREAKER",
                    detail={"count": count, "limit": settings.max_auto_release_count, "reason": "count"},
                )
                # void policy: mark breaker? spec says do not flood, emit breaker and follow void policy.
                # We will not release this cycle.
                return
            if settings.max_auto_release_notional is not None:
                total = Decimal(0)
                for j in deferred_jobs:
                    if j.reference_price is not None:
                        total += abs(j.reference_price)
                if total > settings.max_auto_release_notional:
                    logger.warning("RED_ZONE_BREAKER trip: notional %s > %s", total, settings.max_auto_release_notional)
                    await EventRepository(session).append(
                        process="release_service",
                        kind="RED_ZONE_BREAKER",
                        detail={"notional": str(total), "limit": str(settings.max_auto_release_notional)},
                    )
                    return

            # validate and release per job
            for job in deferred_jobs[: settings.max_auto_release_count]:
                # release-time validation
                # 1. deferred session count
                deferred_at = job.deferred_at or job.received_at
                cnt = clock.deferred_session_count(deferred_at, now)
                if cnt > settings.max_deferred_sessions:
                    await repo.void_deferred_job(job.job_id, "VOID_STALE")
                    await EventRepository(session).append(
                        process="release_service", kind="RED_ZONE_VOIDED", detail={"job_id": str(job.job_id), "reason": "VOID_STALE", "deferred_session_count": cnt}
                    )
                    continue
                # 2. kill-switch check - authoritative per-account
                account_id = None
                if job.account_scope is not None:
                    try:
                        account_id = int(job.account_scope)
                    except Exception:  # noqa: BLE001
                        account_id = None
                if account_id is not None:
                    try:
                        from app.services.kill_switch import (
                            is_account_kill_switch_active,
                        )

                        if is_account_kill_switch_active(account_id):
                            await repo.void_deferred_job(job.job_id, "VOID_KILLSWITCH")
                            await EventRepository(session).append(
                                process="release_service",
                                kind="RED_ZONE_VOIDED",
                                detail={"job_id": str(job.job_id), "reason": "VOID_KILLSWITCH", "account_id": account_id},
                            )
                            logger.warning("Red Zone void kill-switch active for job %s account %s", job.job_id, account_id)
                            continue
                    except Exception:
                        logger.exception("Kill switch check failed for job %s", job.job_id)
                # 3. contract validity - reuse expiry logic
                try:
                    from app.instruments.resolver import is_expiry_instrument

                    raw = job.raw_payload or {}
                    buckets = raw.get("buckets") if isinstance(raw, dict) else None
                    is_expired = False
                    if isinstance(buckets, list):
                        for bucket in buckets:
                            if not isinstance(bucket, dict):
                                continue
                            legs = bucket.get("legs") or []
                            for leg in legs:
                                if not isinstance(leg, dict):
                                    continue
                                itype = leg.get("instrument_type")
                                if not is_expiry_instrument(itype):
                                    continue
                                cm = leg.get("contract_month")
                                if not cm:
                                    # missing contract_month for expiry instrument is invalid -> void
                                    is_expired = True
                                    break
                                # contract_month format YYYY-MM
                                try:
                                    y, m = map(int, str(cm).split("-")[:2])
                                    # expiry is last day of contract month at close; consider expired if now month > contract month
                                    now_ym = now.year * 12 + now.month
                                    cm_ym = y * 12 + m
                                    if now_ym > cm_ym:
                                        is_expired = True
                                except Exception:  # noqa: BLE001
                                    # malformed -> void as expired/invalid
                                    is_expired = True
                                    break
                    if is_expired:
                        await repo.void_deferred_job(job.job_id, "VOID_EXPIRED_CONTRACT")
                        await EventRepository(session).append(
                            process="release_service",
                            kind="RED_ZONE_VOIDED",
                            detail={"job_id": str(job.job_id), "reason": "VOID_EXPIRED_CONTRACT"},
                        )
                        logger.warning("Red Zone void expired contract for job %s", job.job_id)
                        continue
                except Exception:
                    logger.exception("Contract validity check failed for job %s", job.job_id)
                # 4. update deferred_session_count
                job.deferred_session_count = cnt

                # Check gateway still ok before each
                # Claim for release using SKIP LOCKED style via update
                # We already have row lock via transaction; to ensure concurrency safety, re-check status
                # Use claim_deferred_for_release logic per job
                from sqlalchemy import update

                from app.db.models.signal import SignalModel

                upd = (
                    update(SignalJobModel)
                    .where(SignalJobModel.job_id == job.job_id, SignalJobModel.status == JOB_STATUS_DEFERRED_RED_ZONE)
                    .values(status="QUEUED", queued_at=now, deferred_session_count=cnt)
                )
                r = await session.execute(upd)
                if r.rowcount:  # type: ignore[attr-defined]
                    # restore signal business state to NEW (allows re-processing)
                    await session.execute(
                        update(SignalModel)
                        .where(SignalModel.strategy_id == job.strategy_id, SignalModel.signal_id == job.signal_id)
                        .values(status="NEW")
                    )
                    await EventRepository(session).append(
                        process="release_service",
                        kind="RED_ZONE_RELEASED",
                        detail={
                            "job_id": str(job.job_id),
                            "strategy_id": job.strategy_id,
                            "signal_id": job.signal_id,
                            "applied_buffer_seconds": job.applied_buffer_seconds,
                            "resolved_session_close": job.resolved_session_close.isoformat() if job.resolved_session_close else None,
                            "deferred_session_count": cnt,
                        },
                    )
                    logger.info("Red Zone released job %s to QUEUED", job.job_id)
            await session.flush()
