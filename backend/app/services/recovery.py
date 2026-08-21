"""Process crash recovery manager and state reconciliation service."""

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.basket import BasketModel
from app.db.models.signal import (
    JOB_STATUS_CLAIMED,
    JOB_STATUS_PROCESSING,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RECOVERY_REQUIRED,
    SignalJobModel,
)
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
        snapshot_ok = False
        if adapter is not None and hasattr(adapter, "fetch_broker_order_snapshot"):
            snapshot_ok = adapter.fetch_broker_order_snapshot()

        recovered_count = 0
        for job in pending_jobs:
            async with self._session_factory() as session, session.begin():
                repo = SignalJobRepository(session)
                if not snapshot_ok:
                    # If broker snapshot unavailable, requeue job for worker claim
                    await repo.update_status(
                        job.job_id,
                        JOB_STATUS_QUEUED,
                        error="Requeued by startup recovery manager (broker snapshot unavailable)",
                    )
                    recovered_count += 1
                    logger.info(
                        "Recovery manager requeued job_id=%s signal_id=%s", job.job_id, job.signal_id
                    )
                else:
                    # Broker snapshot fetched; requeue for safe worker processing
                    await repo.update_status(
                        job.job_id,
                        JOB_STATUS_QUEUED,
                        error="Requeued by startup recovery manager with broker snapshot",
                    )
                    recovered_count += 1
                    logger.info(
                        "Recovery manager reconciled job_id=%s signal_id=%s to QUEUED",
                        job.job_id,
                        job.signal_id,
                    )

        # Trigger basket critical recovery on order manager if baskets exist
        if hasattr(self._order_manager, "hydrate_runtime_from_db"):
            await self._order_manager.hydrate_runtime_from_db()

        logger.info("Startup recovery completed: %d jobs recovered", recovered_count)
        return recovered_count
