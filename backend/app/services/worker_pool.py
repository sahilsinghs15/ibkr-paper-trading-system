"""Durable execution worker pool for processing signal jobs from PostgreSQL queue."""

import asyncio
import hashlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.identifiers import normalize_strategy_id, normalize_trade_id
from app.core.logger import bind_log_context, clear_log_context
from app.db.models.signal import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PROCESSING,
    JOB_STATUS_RECOVERY_REQUIRED,
    JOB_STATUS_REJECTED,
    SignalJobModel,
)
from app.db.repositories.execution_claim_repository import ExecutionClaimRepository
from app.db.repositories.signal_repository import SignalJobRepository

logger = logging.getLogger(__name__)


def compute_idempotency_key(payload: dict[str, Any]) -> tuple[str, str, str | None, str]:
    """Compute deterministic strategy_id, signal_id, trade_id and SHA256 idempotency key.

    strategy_id is normalized so the digest and the persisted join column do not
    vary with the alert's capitalization. Changing this input rotates the hash,
    so existing rows are backfilled by migration a4c7e2f10938.
    """
    strategy_id = normalize_strategy_id(
        payload.get("strategy") or payload.get("strategy_id")
    )
    trade_id = normalize_trade_id(payload.get("trade_id") or payload.get("signal_id"))
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
        claim_stale_after_sec: float = 300.0,
        idle_poll_interval_sec: float = 0.5,
    ) -> None:
        self._session_factory = session_factory
        self._order_manager = order_manager
        self._worker_count = max(1, worker_count)
        self._lease_duration_sec = lease_duration_sec
        self._reclaim_interval_sec = reclaim_interval_sec
        self._claim_stale_after_sec = claim_stale_after_sec
        self._idle_poll_interval_sec = idle_poll_interval_sec
        self._workers: list[asyncio.Task] = []
        self._reclaimer_task: asyncio.Task | None = None
        self._running = False
        self._domain_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._domain_locks_guard = asyncio.Lock()
        self._in_flight = 0

    def has_in_flight_jobs(self) -> bool:
        """True while any worker is inside _execute_job (live signal path)."""
        return self._in_flight > 0

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
        """Gracefully stop worker pool, waiting for in-flight jobs before cancel."""
        if not self._running:
            return
        self._running = False
        deadline = asyncio.get_running_loop().time() + 90.0
        while self.has_in_flight_jobs() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)
        if self.has_in_flight_jobs():
            logger.warning(
                "Worker pool stop: %s job(s) still in flight after drain wait",
                self._in_flight,
            )
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
        """Periodically reclaim expired job leases and orphaned execution claims."""
        while self._running:
            try:
                await asyncio.sleep(self._reclaim_interval_sec)
                async with self._session_factory() as session, session.begin():
                    stats = await SignalJobRepository(session).reclaim_stale_jobs()
                    if any(stats.values()):
                        logger.warning(
                            "Stale lease sweep: requeued=%d quarantined=%d dead_lettered=%d",
                            stats["requeued"],
                            stats["quarantined"],
                            stats["dead_lettered"],
                        )
                # Claims orphaned by a worker that died mid-execution would
                # otherwise stay held until the next process restart.
                async with self._session_factory() as session, session.begin():
                    claim_stats = await ExecutionClaimRepository(session).reconcile_stale_claims(
                        stale_after_sec=self._claim_stale_after_sec
                    )
                    if any(claim_stats.values()):
                        logger.warning(
                            "Orphaned claim sweep: released=%d sealed=%d",
                            claim_stats["released"],
                            claim_stats["sealed"],
                        )
                async with self._session_factory() as session, session.begin():
                    from app.db.repositories.basket_repository import BasketRepository
                    from app.db.repositories.order_repository import OrderRepository

                    reaped = await BasketRepository(session).reap_stale_executing(
                        older_than_sec=120.0
                    )
                    if reaped:
                        logger.warning("Basket reaper escalated %d aged EXECUTING baskets", reaped)
                    order_reaped = await OrderRepository(session).reap_stale_orders(
                        older_than_sec=120.0
                    )
                    if order_reaped:
                        logger.warning(
                            "Order reaper processed %d aged zero-fill orders", order_reaped
                        )
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
                    await asyncio.sleep(self._idle_poll_interval_sec)
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
        lease_lost = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._lease_heartbeat(job.job_id, worker_id, heartbeat_cancel, lease_lost)
        )

        try:
            async with domain_lock:
                # The domain lock can be held for a while by a sibling job. Re-check
                # ownership before doing any work rather than executing on a lease
                # the reclaimer has already taken away.
                if lease_lost.is_set():
                    logger.warning(
                        "Worker %s abandoning job %s: lease lost while waiting on domain lock",
                        worker_id,
                        job.job_id,
                    )
                    return
                self._in_flight += 1
                try:
                    await self._execute_job(worker_id, job, lease_lost)
                finally:
                    self._in_flight = max(0, self._in_flight - 1)
        finally:
            heartbeat_cancel.set()
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            clear_log_context()

    async def _lease_heartbeat(
        self,
        job_id: Any,
        worker_id: str,
        cancel_event: asyncio.Event,
        lease_lost: asyncio.Event,
    ) -> None:
        """Background task renewing the worker lease while processing a job.

        If the renewal stops matching rows the job has been reclaimed by someone
        else; ``lease_lost`` is set so the executing coroutine can decline to
        write a terminal status it no longer owns.
        """
        interval = max(2.0, self._lease_duration_sec / 3.0)
        while not cancel_event.is_set():
            try:
                await asyncio.sleep(interval)
                if cancel_event.is_set():
                    break
                async with self._session_factory() as session, session.begin():
                    repo = SignalJobRepository(session)
                    renewed = await repo.heartbeat_lease(
                        job_id, worker_id, lease_duration_sec=self._lease_duration_sec
                    )
                if not renewed:
                    lease_lost.set()
                    logger.error(
                        "Worker %s LOST its lease on job %s -- another worker may now own it",
                        worker_id,
                        job_id,
                    )
                    break
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Worker %s failed to renew heartbeat for job %s; treating as lease_lost",
                    worker_id,
                    job_id,
                )
                lease_lost.set()
                break

    async def _write_status(
        self,
        job_id: Any,
        status: str,
        worker_id: str,
        lease_lost: asyncio.Event,
        *,
        error: str | None = None,
    ) -> bool:
        """Write a fenced job status. Returns False if this worker no longer owns the job."""
        async with self._session_factory() as session, session.begin():
            rows = await SignalJobRepository(session).update_status(
                job_id,
                status,
                error=error,
                worker_id=worker_id,
                fence=True,
                lease_duration_sec=self._lease_duration_sec,
            )
        if not rows:
            lease_lost.set()
            logger.error(
                "Worker %s could not write status=%s for job %s: lease no longer held",
                worker_id,
                status,
                job_id,
            )
            return False
        return True

    async def _execute_job(
        self, worker_id: str, job: SignalJobModel, lease_lost: asyncio.Event
    ) -> None:
        """Invoke OrderManager execution pipeline and update job completion status."""
        logger.info(
            "Worker %s starting execution for job_id=%s signal_id=%s strategy_id=%s attempt=%d",
            worker_id,
            job.job_id,
            job.signal_id,
            job.strategy_id,
            job.attempt_count,
        )

        # Mark job status as PROCESSING. This also extends the lease, and the fence
        # means we bail out here rather than executing on a lease we already lost.
        if not await self._write_status(
            job.job_id, JOB_STATUS_PROCESSING, worker_id, lease_lost
        ):
            return

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
            await self._write_status(
                job.job_id, JOB_STATUS_REJECTED, worker_id, lease_lost, error=str(exc)
            )
            return

        try:
            execution = await self._order_manager.process_signal_execution(domain_signal)

            if lease_lost.is_set():
                # Orders may have gone out under a lease another worker now owns.
                # Do not write a terminal status -- leave it for reconciliation.
                logger.error(
                    "Worker %s finished job %s after losing its lease; leaving status for recovery",
                    worker_id,
                    job.job_id,
                )
                return

            if execution is not None and getattr(execution, "had_unexpected_error", False):
                logger.error(
                    "Worker %s: signal %s had unexpected fan-out error; quarantining for reconciliation",
                    worker_id,
                    job.signal_id,
                )
                await self._write_status(
                    job.job_id,
                    JOB_STATUS_RECOVERY_REQUIRED,
                    worker_id,
                    lease_lost,
                    error="Unexpected error during multi-account fan-out; reconciliation required.",
                )
                return

            if execution is not None and getattr(execution, "all_rejected", False):
                logger.warning(
                    "Worker %s: signal %s rejected by RMS/OMS policy", worker_id, job.signal_id
                )
                await self._write_status(
                    job.job_id,
                    JOB_STATUS_REJECTED,
                    worker_id,
                    lease_lost,
                    error="Execution rejected by RMS/OMS policy",
                )
                return

            if execution is not None and not getattr(execution, "success", True):
                err_msg = getattr(execution, "error_message", None) or "Execution incomplete"
                logger.warning(
                    "Worker %s: signal %s execution incomplete: %s",
                    worker_id,
                    job.signal_id,
                    err_msg,
                )
                await self._write_status(
                    job.job_id, JOB_STATUS_FAILED, worker_id, lease_lost, error=err_msg
                )
                return

            logger.info(
                "Worker %s completed job_id=%s signal_id=%s successfully",
                worker_id,
                job.job_id,
                job.signal_id,
            )
            await self._write_status(
                job.job_id, JOB_STATUS_COMPLETED, worker_id, lease_lost
            )
        except Exception as exc:
            logger.exception("Worker %s failed executing job %s", worker_id, job.job_id)
            terminal_status = JOB_STATUS_FAILED
            error_msg = str(exc)
            try:
                async with self._session_factory() as session:
                    emitted = await SignalJobRepository(session).count_orders_emitted(
                        job.strategy_id, job.signal_id
                    )
                    claimed = await ExecutionClaimRepository(session).has_claimed(
                        job.strategy_id, job.signal_id
                    )
                if emitted > 0 or claimed:
                    terminal_status = JOB_STATUS_RECOVERY_REQUIRED
                    error_msg = (
                        f"{exc} — {emitted} order(s) already emitted; reconciliation required."
                    )
            except Exception:
                logger.exception(
                    "Worker %s could not count emitted orders for job %s",
                    worker_id,
                    job.job_id,
                )
            await self._write_status(
                job.job_id, terminal_status, worker_id, lease_lost, error=error_msg
            )
