"""Process crash recovery manager and state reconciliation service."""

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.basket import BasketModel
from app.db.models.signal import (
    JOB_STATUS_CLAIMED,
    JOB_STATUS_DEAD_LETTER,
    JOB_STATUS_PROCESSING,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RECOVERY_REQUIRED,
    SignalJobModel,
)
from app.db.repositories.execution_claim_repository import ExecutionClaimRepository
from app.db.repositories.signal_repository import SignalJobRepository

logger = logging.getLogger(__name__)


class RecoveryManager:
    """Discovers non-terminal signal jobs and baskets on startup and reconciles broker state."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        order_manager: Any,
    ) -> None:
        self._session_factory = session_factory
        self._order_manager = order_manager

    async def run_startup_recovery(self) -> int:
        """Scan PostgreSQL for non-terminal execution jobs and reconcile state on app startup.

        Returns the number of jobs recovered.
        """
        async with self._session_factory() as session, session.begin():
            stmt = select(SignalJobModel).where(
                SignalJobModel.status.in_(
                    [JOB_STATUS_CLAIMED, JOB_STATUS_PROCESSING, JOB_STATUS_RECOVERY_REQUIRED]
                )
            )
            result = await session.execute(stmt)
            pending_jobs = list(result.scalars().all())

            basket_stmt = select(BasketModel).where(
                BasketModel.state.in_(["EXECUTING", "UNWINDING"])
            )
            baskets_res = await session.execute(basket_stmt)
            pending_baskets = list(baskets_res.scalars().all())

        if not pending_jobs and not pending_baskets:
            logger.info("Startup recovery scan complete: no pending jobs or baskets found")
            return 0

        logger.info(
            "Startup recovery scan found %d pending jobs and %d incomplete baskets",
            len(pending_jobs),
            len(pending_baskets),
        )

        adapter = getattr(getattr(self._order_manager, "_oms", None), "_adapter", None)
        if adapter is not None and hasattr(adapter, "fetch_broker_order_snapshot"):
            # Best-effort only: reqOpenOrders/reqExecutions are fire-and-forget, the
            # callbacks land asynchronously. Requesting it warms adapter state but
            # tells us nothing synchronously, so it must not gate the decision below.
            if adapter.fetch_broker_order_snapshot():
                logger.info("Requested open orders / executions snapshot from broker")
            else:
                logger.warning("Broker snapshot unavailable at recovery time")

        # Resolve execution claims left held by the crashed process before any
        # requeued job can try to take them again.
        async with self._session_factory() as session, session.begin():
            claim_stats = await ExecutionClaimRepository(session).reconcile_stale_claims(
                stale_after_sec=0.0
            )
        if any(claim_stats.values()):
            logger.warning(
                "Execution claim reconciliation: released=%d sealed=%d",
                claim_stats["released"],
                claim_stats["sealed"],
            )

        requeued = 0
        quarantined = 0
        for job in pending_jobs:
            async with self._session_factory() as session, session.begin():
                repo = SignalJobRepository(session)
                emitted = await repo.count_orders_emitted(job.strategy_id, job.signal_id)

                if emitted:
                    # This job already wrote orders to the ledger before the crash.
                    # Requeueing would re-send them, so quarantine for explicit
                    # reconciliation instead.
                    await repo.update_status(
                        job.job_id,
                        JOB_STATUS_RECOVERY_REQUIRED,
                        error=(
                            f"Crash recovery: {emitted} order(s) already emitted; "
                            "requires reconciliation before retry."
                        ),
                    )
                    quarantined += 1
                    logger.warning(
                        "Recovery quarantined job_id=%s signal_id=%s (%d orders already emitted)",
                        job.job_id,
                        job.signal_id,
                        emitted,
                    )
                    continue

                if job.attempt_count >= job.max_attempts:
                    await repo.update_status(
                        job.job_id,
                        JOB_STATUS_DEAD_LETTER,
                        error=f"Crash recovery: exceeded max attempts ({job.max_attempts}).",
                    )
                    quarantined += 1
                    logger.warning(
                        "Recovery dead-lettered job_id=%s signal_id=%s after %d attempts",
                        job.job_id,
                        job.signal_id,
                        job.attempt_count,
                    )
                    continue

                # No orders emitted: the job never reached the broker, safe to retry.
                await repo.update_status(
                    job.job_id,
                    JOB_STATUS_QUEUED,
                    error="Requeued by startup recovery manager (no orders emitted).",
                )
                requeued += 1
                logger.info(
                    "Recovery requeued job_id=%s signal_id=%s", job.job_id, job.signal_id
                )

        recovered_count = requeued + quarantined
        logger.info(
            "Startup recovery complete: requeued=%d quarantined=%d", requeued, quarantined
        )

        # Trigger basket critical recovery on order manager if baskets exist
        if hasattr(self._order_manager, "hydrate_runtime_from_db"):
            await self._order_manager.hydrate_runtime_from_db()

        logger.info("Startup recovery completed: %d jobs recovered", recovered_count)
        return recovered_count
