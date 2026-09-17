"""Durable delivery worker consuming pending channel deliveries from PostgreSQL outbox."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.db.models.notification import NotificationDeliveryModel, NotificationLogModel
from app.services.notification.dispatcher import (
    ChannelDispatcher,
    get_default_dispatcher,
)
from app.services.notification.types import (
    DeliveryResult,
    DeliveryStatus,
    NotificationStatus,
)

logger = logging.getLogger(__name__)


class NotificationDeliveryWorker:
    """Consumes and dispatches pending notification deliveries with concurrency safety."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        dispatcher: ChannelDispatcher | None = None,
        *,
        poll_interval_sec: float = 1.0,
        batch_size: int = 10,
        max_retries: int = 3,
        initial_retry_backoff_sec: float = 10.0,
    ) -> None:
        self._session_factory = session_factory
        self._dispatcher = dispatcher or get_default_dispatcher()
        self._poll_interval_sec = max(0.1, poll_interval_sec)
        self._batch_size = max(1, batch_size)
        self._max_retries = max(1, max_retries)
        self._initial_retry_backoff_sec = max(1.0, initial_retry_backoff_sec)

        self._running = False
        self._worker_task: asyncio.Task[None] | None = None
        self._last_recovery_ts: float = 0.0

    async def start(self) -> None:
        """Start the background delivery worker loop."""
        if self._running:
            return
        self._running = True
        try:
            await self.recover_stuck_deliveries()
        except Exception:
            logger.exception("Initial stuck delivery recovery encountered error")
        self._worker_task = asyncio.create_task(
            self._worker_loop(), name="notification-delivery-worker"
        )
        logger.info("NotificationDeliveryWorker started (poll_interval=%.1fs)", self._poll_interval_sec)

    async def stop(self) -> None:
        """Gracefully stop the delivery worker loop."""
        if not self._running:
            return
        self._running = False
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("NotificationDeliveryWorker stopped")

    async def _worker_loop(self) -> None:
        """Continuously poll and process pending deliveries."""
        while self._running:
            try:
                processed = await self.run_once(batch_size=self._batch_size)
                if processed == 0:
                    await asyncio.sleep(self._poll_interval_sec)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Unexpected error in NotificationDeliveryWorker loop")
                await asyncio.sleep(self._poll_interval_sec)

    async def recover_stuck_deliveries(self, lease_timeout_sec: float = 60.0) -> int:
        """Recover deliveries stuck in SENDING status past lease_timeout_sec.

        Transitions recoverable items to RETRYING (with immediate next_retry_at),
        and exhausted items (attempt_count >= max_retries) to FAILED.
        Returns the number of recovered items.
        """
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=max(5.0, lease_timeout_sec))
        recovered_count = 0

        async with self._session_factory() as session, session.begin():
            stuck_query = (
                select(NotificationDeliveryModel)
                .where(
                    NotificationDeliveryModel.status == DeliveryStatus.SENDING.value,
                    NotificationDeliveryModel.last_attempt_at < cutoff,
                )
                .with_for_update(skip_locked=True)
            )
            stuck_rows = list((await session.execute(stuck_query)).scalars().all())
            if not stuck_rows:
                return 0

            for deliv in stuck_rows:
                effective_max = min(deliv.max_retries, self._max_retries)
                if deliv.attempt_count >= effective_max:
                    deliv.status = DeliveryStatus.FAILED.value
                    deliv.next_retry_at = None
                    error_info = deliv.error_details or {}
                    error_info["reason"] = "STUCK_LEASE_EXHAUSTED"
                    deliv.error_details = error_info
                    logger.error(
                        "Stuck delivery %s exceeded max retries (%d/%d), marked FAILED",
                        deliv.delivery_id,
                        deliv.attempt_count,
                        effective_max,
                    )
                    await self._check_and_update_parent_status(session, deliv.notification_id)
                else:
                    deliv.status = DeliveryStatus.RETRYING.value
                    deliv.next_retry_at = now
                    logger.warning(
                        "Recovered stuck delivery %s (attempt %d/%d), rescheduled immediately",
                        deliv.delivery_id,
                        deliv.attempt_count,
                        effective_max,
                    )
                recovered_count += 1

        return recovered_count

    async def run_once(self, batch_size: int = 10) -> int:
        """Process a single batch of eligible deliveries.

        Returns the number of claimed and processed deliveries.
        Never holds database locks during external network calls.
        """
        now = datetime.now(UTC)

        # Periodically recover stuck deliveries (e.g. every 30 seconds)
        loop_ts = now.timestamp()
        if loop_ts - self._last_recovery_ts > 30.0:
            self._last_recovery_ts = loop_ts
            try:
                await self.recover_stuck_deliveries()
            except Exception:
                logger.exception("Periodic stuck delivery recovery encountered error")

        # 1. Claim eligible delivery items with FOR UPDATE SKIP LOCKED
        claimed_items: list[tuple[int, int]] = []  # (delivery_pk, notification_pk)

        async with self._session_factory() as session, session.begin():
            claim_query = (
                select(NotificationDeliveryModel.id, NotificationDeliveryModel.notification_id)
                .where(
                    NotificationDeliveryModel.status.in_(
                        [DeliveryStatus.PENDING.value, DeliveryStatus.RETRYING.value]
                    ),
                    (NotificationDeliveryModel.next_retry_at.is_(None))
                    | (NotificationDeliveryModel.next_retry_at <= now),
                )
                .order_by(NotificationDeliveryModel.id.asc())
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            rows = (await session.execute(claim_query)).all()
            if not rows:
                return 0

            claimed_ids = [r[0] for r in rows]
            claimed_items = [(r[0], r[1]) for r in rows]

            # Mark claimed items as SENDING within the claim transaction
            claim_update = (
                update(NotificationDeliveryModel)
                .where(NotificationDeliveryModel.id.in_(claimed_ids))
                .values(
                    status=DeliveryStatus.SENDING.value,
                    last_attempt_at=now,
                    attempt_count=NotificationDeliveryModel.attempt_count + 1,
                )
            )
            await session.execute(claim_update)
            # Transaction commits here, releasing row locks BEFORE external HTTP calls

        # 2. Dispatch each delivery outside of database transactions
        for deliv_id, _notif_id in claimed_items:
            await self._process_single_delivery(deliv_id)

        return len(claimed_items)

    async def _process_single_delivery(self, delivery_pk: int) -> None:
        """Load single delivery and logical record, dispatch, and record outcome."""
        # Load delivery and associated logical notification
        delivery: NotificationDeliveryModel | None = None
        logical: NotificationLogModel | None = None

        async with self._session_factory() as session:
            stmt = (
                select(NotificationDeliveryModel)
                .options(selectinload(NotificationDeliveryModel.notification))
                .where(NotificationDeliveryModel.id == delivery_pk)
            )
            delivery = (await session.execute(stmt)).scalar_one_or_none()
            if delivery is not None:
                logical = delivery.notification

        if delivery is None or logical is None:
            logger.warning("Delivery %s or associated notification not found", delivery_pk)
            return

        # Dispatch via ChannelDispatcher
        try:
            result = await self._dispatcher.dispatch(delivery, logical)
        except Exception as exc:
            logger.exception("Dispatcher crashed for delivery %s", delivery_pk)
            result = DeliveryResult(
                success=False,
                error_message=f"Dispatcher crashed: {exc}",
                error_details={"exception": str(exc), "type": type(exc).__name__},
                is_retryable=True,
            )
        record_time = datetime.now(UTC)

        async with self._session_factory() as session, session.begin():
            # Reload delivery under lock to update outcome
            deliv_record = await session.get(NotificationDeliveryModel, delivery_pk)
            if deliv_record is None:
                return

            if result.success:
                deliv_record.status = DeliveryStatus.DELIVERED.value
                deliv_record.provider_message_id = result.provider_message_id
                deliv_record.delivered_at = record_time
                deliv_record.error_details = None
                deliv_record.next_retry_at = None

                # Check if all deliveries for this notification are completed
                await self._check_and_update_parent_status(session, deliv_record.notification_id)
            else:
                error_info = result.error_details or {}
                if result.error_message:
                    error_info["error_message"] = result.error_message

                # Determine whether retry is eligible
                effective_max_retries = min(deliv_record.max_retries, self._max_retries)
                if result.is_retryable and deliv_record.attempt_count < effective_max_retries:
                    deliv_record.status = DeliveryStatus.RETRYING.value
                    deliv_record.error_details = error_info
                    # Exponential backoff: base * 2^(attempt - 1)
                    backoff_factor = 2 ** max(0, deliv_record.attempt_count - 1)
                    backoff_sec = self._initial_retry_backoff_sec * backoff_factor

                    # Respect provider retry_after (e.g. Telegram HTTP 429) if specified
                    if error_info and "retry_after" in error_info:
                        try:
                            backoff_sec = max(backoff_sec, float(error_info["retry_after"]))
                        except (ValueError, TypeError):
                            pass

                    deliv_record.next_retry_at = record_time + timedelta(seconds=backoff_sec)
                    logger.warning(
                        "Delivery %s failed (retryable): attempt %d/%d, next retry in %.1fs",
                        deliv_record.delivery_id,
                        deliv_record.attempt_count,
                        effective_max_retries,
                        backoff_sec,
                    )
                else:
                    deliv_record.status = DeliveryStatus.FAILED.value
                    deliv_record.error_details = error_info
                    deliv_record.next_retry_at = None
                    logger.error(
                        "Delivery %s permanently failed: %s (attempt %d/%d)",
                        deliv_record.delivery_id,
                        result.error_message,
                        deliv_record.attempt_count,
                        effective_max_retries,
                    )
                    await self._check_and_update_parent_status(session, deliv_record.notification_id)

    async def _check_and_update_parent_status(
        self,
        session: AsyncSession,
        notification_id: int,
    ) -> None:
        """Update parent NotificationLogModel status if all deliveries reached terminal states."""
        await session.flush()
        stmt = select(NotificationDeliveryModel.status).where(
            NotificationDeliveryModel.notification_id == notification_id
        )
        statuses = [s for s in (await session.execute(stmt)).scalars().all()]
        if not statuses:
            return

        # If all deliveries are DELIVERED -> COMPLETED
        if all(s == DeliveryStatus.DELIVERED.value for s in statuses):
            await session.execute(
                update(NotificationLogModel)
                .where(NotificationLogModel.id == notification_id)
                .values(status=NotificationStatus.COMPLETED.value)
            )
        # If all deliveries reached terminal failure -> FAILED
        elif all(s in (DeliveryStatus.FAILED.value, DeliveryStatus.ABANDONED.value) for s in statuses):
            await session.execute(
                update(NotificationLogModel)
                .where(NotificationLogModel.id == notification_id)
                .values(status=NotificationStatus.FAILED.value)
            )
        # If some are delivered and some failed, but none are pending/retrying/sending -> DISPATCHED
        elif all(s in (DeliveryStatus.DELIVERED.value, DeliveryStatus.FAILED.value, DeliveryStatus.ABANDONED.value) for s in statuses):
            await session.execute(
                update(NotificationLogModel)
                .where(NotificationLogModel.id == notification_id)
                .values(status=NotificationStatus.DISPATCHED.value)
            )
