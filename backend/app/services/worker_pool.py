"""Durable execution worker pool for processing signal jobs from PostgreSQL queue."""

import asyncio
import hashlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logger import bind_log_context, clear_log_context
from app.db.models.signal import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PROCESSING,
    JOB_STATUS_REJECTED,
    SignalJobModel,
)
from app.db.repositories.signal_repository import SignalJobRepository

logger = logging.getLogger(__name__)


def compute_idempotency_key(payload: dict[str, Any]) -> tuple[str, str, str | None, str]:
    """Compute deterministic strategy_id, signal_id, trade_id and SHA256 idempotency key."""
    strategy_id = str(
        payload.get("strategy") or payload.get("strategy_id") or "default_strategy"
    ).strip()
    trade_id = str(payload.get("trade_id") or payload.get("signal_id") or "").strip()
    action = str(payload.get("action") or "").strip().upper()
    signal_id = trade_id
    if action == "CLOSE" and trade_id and not trade_id.endswith(":CLOSE"):
        signal_id = f"{trade_id}:CLOSE"
    if not signal_id:
        signal_id = f"SIG-{uuid.uuid4().hex[:12].upper()}"

    key_raw = f"{strategy_id}:{signal_id}:{action}"
    idempotency_key = hashlib.sha256(key_raw.encode("utf-8")).hexdigest()
    return strategy_id, signal_id, trade_id or None, idempotency_key


class ExecutionWorkerPool:
    """Manages concurrent worker tasks claiming and executing queued signal jobs."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        order_manager: Any,
        *,
        worker_count: int = 10,
        lease_duration_sec: float = 30.0,
        reclaim_interval_sec: float = 15.0,
    ) -> None:
        self._session_factory = session_factory
        self._order_manager = order_manager
        self._worker_count = max(1, worker_count)
        self._lease_duration_sec = lease_duration_sec
        self._reclaim_interval_sec = reclaim_interval_sec
        self._workers: list[asyncio.Task] = []
        self._reclaimer_task: asyncio.Task | None = None
        self._running = False
        self._domain_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._domain_locks_guard = asyncio.Lock()

    async def _get_domain_lock(self, account_scope: str | None, strategy_id: str) -> asyncio.Lock:
        """Get or create an async lock for (account_scope, strategy_id) partition safety."""
        key = (account_scope or "default", strategy_id)
        async with self._domain_locks_guard:
            if key not in self._domain_locks:
                self._domain_locks[key] = asyncio.Lock()
            return self._domain_locks[key]

    async def start(self) -> None:
        """Start worker tasks and stale job reclaimer loop."""
        if self._running:
            return
        self._running = True
        self._workers = [
            asyncio.create_task(self._worker_loop(f"worker-{i + 1}"), name=f"mft-worker-{i + 1}")
            for i in range(self._worker_count)
        ]
        self._reclaimer_task = asyncio.create_task(
            self._reclaimer_loop(), name="mft-stale-job-reclaimer"
        )
        logger.info("ExecutionWorkerPool started with %d workers", self._worker_count)

    async def stop(self) -> None:
        """Gracefully stop worker pool."""
        if not self._running:
            return
        self._running = False
        if self._reclaimer_task is not None:
            self._reclaimer_task.cancel()
            try:
                await self._reclaimer_task
            except asyncio.CancelledError:
                pass
        for w in self._workers:
            w.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        logger.info("ExecutionWorkerPool stopped")

    async def _reclaimer_loop(self) -> None:
        """Periodically reclaim jobs with expired worker leases."""
        while self._running:
            try:
                await asyncio.sleep(self._reclaim_interval_sec)
                async with self._session_factory() as session, session.begin():
                    reclaimed = await SignalJobRepository(session).reclaim_stale_jobs()
                    if reclaimed > 0:
                        logger.warning("Reclaimed %d stale signal jobs from expired leases", reclaimed)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in stale job reclaimer loop")

    async def _worker_loop(self, worker_id: str) -> None:
        """Main loop for an individual worker claiming and executing jobs."""
        while self._running:
            try:
                jobs = await self._claim_job(worker_id)
                if not jobs:
                    await asyncio.sleep(0.05)
                    continue

                for job in jobs:
                    await self._process_claimed_job(worker_id, job)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Unexpected error in worker loop %s", worker_id)
                await asyncio.sleep(0.5)

    async def _claim_job(self, worker_id: str) -> list[SignalJobModel]:
        """Claim a single queued job from Postgres."""
        async with self._session_factory() as session, session.begin():
            repo = SignalJobRepository(session)
            return await repo.claim_next_jobs(
                worker_id, limit=1, lease_duration_sec=self._lease_duration_sec
            )

    async def _process_claimed_job(self, worker_id: str, job: SignalJobModel) -> None:
        """Execute a claimed job under its domain partition lock with lease heartbeat."""
        domain_lock = await self._get_domain_lock(job.account_scope, job.strategy_id)
        bind_log_context(
            request_id=job.correlation_id,
            signal_id=job.signal_id,
            trade_id=job.trade_id or job.signal_id,
        )

        heartbeat_cancel = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._lease_heartbeat(job.job_id, worker_id, heartbeat_cancel)
        )

        try:
            async with domain_lock:
                await self._execute_job(worker_id, job)
        finally:
            heartbeat_cancel.set()
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            clear_log_context()

    async def _lease_heartbeat(
        self, job_id: Any, worker_id: str, cancel_event: asyncio.Event
    ) -> None:
        """Background task renewing the worker lease while processing a job."""
        interval = max(5.0, self._lease_duration_sec / 3.0)
        while not cancel_event.is_set():
            try:
                await asyncio.sleep(interval)
                if cancel_event.is_set():
                    break
                async with self._session_factory() as session, session.begin():
                    repo = SignalJobRepository(session)
                    await repo.heartbeat_lease(
                        job_id, worker_id, lease_duration_sec=self._lease_duration_sec
                    )
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                logger.warning("Worker %s failed to renew heartbeat for job %s", worker_id, job_id)

    async def _execute_job(self, worker_id: str, job: SignalJobModel) -> None:
        """Invoke OrderManager execution pipeline and update job completion status."""
        logger.info(
            "Worker %s starting execution for job_id=%s signal_id=%s strategy_id=%s attempt=%d",
            worker_id,
            job.job_id,
            job.signal_id,
            job.strategy_id,
            job.attempt_count,
        )

        # Mark job status as PROCESSING
        async with self._session_factory() as session, session.begin():
            await SignalJobRepository(session).update_status(
                job.job_id, JOB_STATUS_PROCESSING, worker_id=worker_id
            )

        utc_now = datetime.now(UTC)
        try:
            domain_signal = self._order_manager.parse_inbound_payload(
                job.raw_payload,
                timestamp=utc_now,
                request_id=job.correlation_id,
                capture_data=job.capture_data or {},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Worker %s rejected job %s: invalid payload parse: %s", worker_id, job.job_id, exc
            )
            async with self._session_factory() as session, session.begin():
                await SignalJobRepository(session).update_status(
                    job.job_id, JOB_STATUS_REJECTED, error=str(exc), worker_id=worker_id
                )
            return

        try:
            execution = await self._order_manager.process_signal_execution(domain_signal)
            if execution is not None and getattr(execution, "all_rejected", False):
                logger.warning(
                    "Worker %s: signal %s rejected by RMS/OMS policy", worker_id, job.signal_id
                )
                async with self._session_factory() as session, session.begin():
                    await SignalJobRepository(session).update_status(
                        job.job_id,
                        JOB_STATUS_REJECTED,
                        error="Execution rejected by RMS/OMS policy",
                        worker_id=worker_id,
                    )
                return

            if execution is not None and not getattr(execution, "success", True):
                err_msg = getattr(execution, "error_message", None) or "Execution incomplete"
                logger.warning("Worker %s: signal %s execution incomplete: %s", worker_id, job.signal_id, err_msg)
                async with self._session_factory() as session, session.begin():
                    await SignalJobRepository(session).update_status(
                        job.job_id, JOB_STATUS_FAILED, error=err_msg, worker_id=worker_id
                    )
                return

            logger.info("Worker %s completed job_id=%s signal_id=%s successfully", worker_id, job.job_id, job.signal_id)
            async with self._session_factory() as session, session.begin():
                await SignalJobRepository(session).update_status(
                    job.job_id, JOB_STATUS_COMPLETED, worker_id=worker_id
                )
        except Exception as exc:
            logger.exception("Worker %s failed executing job %s", worker_id, job.job_id)
            async with self._session_factory() as session, session.begin():
                await SignalJobRepository(session).update_status(
                    job.job_id, JOB_STATUS_FAILED, error=str(exc), worker_id=worker_id
                )
