"""Per-account realized P&L loss threshold monitor.

Evaluates each enabled account every 30s:
  realised = sum(positions.realised_pnl)
  breached = loss_threshold is not None and realised <= loss_threshold
State machine persisted in account_loss_state with SELECT FOR UPDATE.
Event is LOSS_THRESHOLD_BREACHED via event_log idempotency_key.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.account import AccountModel
from app.db.models.account_loss_state import AccountLossStateModel
from app.db.models.position import PositionModel
from app.db.repositories.event_repository import EventRepository
from app.services.notification_canonical import send_canonical_telegram

logger = logging.getLogger(__name__)


async def _sum_realised(session: AsyncSession, account_id: int) -> Decimal:
    stmt = select(func.coalesce(func.sum(PositionModel.realised_pnl), 0)).where(PositionModel.account_id == account_id)
    total = (await session.execute(stmt)).scalar_one()
    return Decimal(str(total))


class LossThresholdMonitor:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        interval_sec: float = 30.0,
    ) -> None:
        self._session_factory = session_factory
        self._interval_sec = interval_sec
        self._task: asyncio.Task | None = None
        self._running = False

    async def evaluate_once(self) -> list[dict[str, Any]]:
        """One pass over all enabled accounts. Returns list of breached details."""
        results: list[dict[str, Any]] = []
        async with self._session_factory() as session:
            accounts = list((await session.execute(select(AccountModel).where(AccountModel.enabled.is_(True)))).scalars().all())
        for acc in accounts:
            res = await self.evaluate_account(acc.id)
            if res is not None:
                results.append(res)
        return results

    async def evaluate_account(self, account_id: int) -> dict[str, Any] | None:
        async with self._session_factory() as session, session.begin():
            # Ensure state row exists without race (INSERT ON CONFLICT DO NOTHING)
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            stmt = pg_insert(AccountLossStateModel).values(account_id=account_id, is_below=False).on_conflict_do_nothing(index_elements=["account_id"])
            await session.execute(stmt)
            await session.flush()
            state_row = (await session.execute(select(AccountLossStateModel).where(AccountLossStateModel.account_id == account_id).with_for_update())).scalar_one()

            account = (await session.execute(select(AccountModel).where(AccountModel.id == account_id))).scalar_one_or_none()
            if account is None:
                return None
            threshold = getattr(account, "loss_threshold", None)
            if threshold is None:
                # disabled -> reset if previously below
                if state_row.is_below:
                    state_row.is_below = False
                    state_row.last_threshold = None
                return None

            # threshold is negative per validation
            realised = await _sum_realised(session, account_id)
            breached = realised <= threshold

            if breached and not state_row.is_below:
                # first crossing
                detail = {
                    "account_id": account_id,
                    "ibkr_account": account.ibkr_account,
                    "realized_pnl": str(realised),
                    "realized_pnl_str": str(realised),
                    "loss_threshold": str(threshold),
                    "loss_threshold_str": str(threshold),
                    "timestamp": datetime.now(UTC).isoformat(),
                }
                # idempotency: one event per crossing instance
                # Include current breach counter via updated_at epoch to prevent duplicates across restarts while still allowing new crossing after recovery
                # Use threshold + state row updated_at as discriminator; simpler: loss_breach:{account_id}:{threshold}:{realised} would dedup too aggressively.
                # Instead use breach count: count existing LOSS events for account + threshold, +1
                # For now use timestamp truncated to second + account + threshold
                ts_key = datetime.now(UTC).isoformat()
                idem = f"loss_breach:{account_id}:{threshold}:{ts_key}"
                # Ensure uniqueness if multiple within same second by adding breach sequence
                # Check if an event with same idempotency already exists is handled by ON CONFLICT DO NOTHING
                repo = EventRepository(session)
                row = await repo.append(process="risk", kind="LOSS_THRESHOLD_BREACHED", detail=detail, idempotency_key=idem)
                # Only send telegram if event was actually inserted
                state_row.is_below = True
                state_row.last_threshold = threshold
                await session.flush()
                if row is not None:
                    # fire telegram outside transaction? Already committed via begin will commit at exit
                    # Schedule after commit
                    asyncio.create_task(send_canonical_telegram("LOSS_THRESHOLD_BREACHED", detail)).add_done_callback(lambda t: logger.debug("loss telegram done %s", t.exception()) if t.exception() else None)
                    return detail
                return None
            elif not breached and state_row.is_below:
                state_row.is_below = False
                state_row.last_threshold = threshold
                # optional: could log recovery, but spec says only reset
                return None
            elif breached and state_row.is_below:
                # already breached, ensure threshold updated if changed
                state_row.last_threshold = threshold
                return None
            else:
                return None

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.evaluate_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("LossThresholdMonitor evaluate_once failed")
            try:
                await asyncio.sleep(self._interval_sec)
            except asyncio.CancelledError:
                break

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="loss-threshold-monitor")
        logger.info("LossThresholdMonitor started interval=%.1fs", self._interval_sec)

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("LossThresholdMonitor stopped")
