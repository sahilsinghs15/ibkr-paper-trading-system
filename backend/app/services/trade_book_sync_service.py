"""Trade Book 10s sync - same-day Gateway executions only, per-account non-overlapping."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.broker.trade_book_sources import CurrentGatewayExecutionSource
from app.db.models.account import AccountModel
from app.db.repositories.trade_execution_repository import TradeExecutionRepository

logger = logging.getLogger(__name__)


class TradeBookSyncService:
    """Polls IBKR current-day executions every 10s per account, upserting into trade_executions."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client,
        *,
        interval_sec: float = 10.0,
    ) -> None:
        self._session_factory = session_factory
        self._client = client
        self._interval = float(interval_sec)
        self._locks: dict[int, asyncio.Lock] = {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_sync_at: dict[int, datetime] = {}
        self._last_success_at: dict[int, datetime] = {}
        self._current = CurrentGatewayExecutionSource(client)

    async def _get_accounts(self) -> list[AccountModel]:
        async with self._session_factory() as session:
            rows = (await session.execute(select(AccountModel).where(AccountModel.enabled.is_(True)))).scalars().all()
            return list(rows)

    def _lock_for(self, account_id: int) -> asyncio.Lock:
        if account_id not in self._locks:
            self._locks[account_id] = asyncio.Lock()
        return self._locks[account_id]

    async def _resolve_order_map(self, account_id: int) -> dict[str, int]:
        from app.db.models.order import OrderModel
        order_map: dict[str, int] = {}
        try:
            async with self._session_factory() as session:
                rows = (await session.execute(select(OrderModel.id, OrderModel.broker_order_id).where(OrderModel.account_id == account_id, OrderModel.broker_order_id.is_not(None)))).all()
                for oid, boid in rows:
                    if boid:
                        order_map[str(boid)] = int(oid)
        except Exception:
            logger.exception("TradeBook order_map resolve failed account_id=%s", account_id)
        return order_map

    async def sync_one_account(self, account: AccountModel) -> tuple[int, bool]:
        ibkr = account.ibkr_account.strip().upper()
        lock = self._lock_for(account.id)
        if lock.locked():
            logger.info("TradeBook sync skip account=%s already in progress", ibkr)
            return 0, False
        async with lock:
            if not self._client.is_connected():
                logger.info("TradeBook sync skip account=%s gateway down", ibkr)
                return 0, False
            try:
                result = await self._current.fetch(ibkr_account=ibkr)
            except Exception:
                logger.exception("TradeBook fetch failed account=%s", ibkr)
                return 0, True
            order_map = await self._resolve_order_map(account.id)
            async with self._session_factory() as session, session.begin():
                repo = TradeExecutionRepository(session)
                await repo.upsert_batch(result.lines, account_id=account.id, order_map=order_map or None)
            self._last_sync_at[account.id] = datetime.now(UTC)
            if not result.timed_out:
                self._last_success_at[account.id] = datetime.now(UTC)
            logger.info("TradeBook sync account=%s fetched=%d timed_out=%s", ibkr, len(result.lines), result.timed_out)
            return len(result.lines), result.timed_out

    async def sync_all_once(self) -> None:
        accounts = await self._get_accounts()
        for acc in accounts:
            try:
                await self.sync_one_account(acc)
            except Exception:
                logger.exception("TradeBook sync_all_once account=%s failed", acc.ibkr_account)

    async def _loop(self) -> None:
        logger.info("TradeBookSyncService loop started interval=%.1fs", self._interval)
        await asyncio.sleep(2.0)
        try:
            await self.sync_all_once()
        except Exception:
            logger.exception("TradeBook initial sync failed")
        while not self._stop.is_set():
            start = asyncio.get_event_loop().time()
            try:
                await self.sync_all_once()
            except Exception:
                logger.exception("TradeBook periodic sync cycle failed")
            elapsed = asyncio.get_event_loop().time() - start
            sleep_for = max(0.0, self._interval - elapsed)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
            except TimeoutError:
                pass
        logger.info("TradeBookSyncService loop stopped")

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="trade-book-sync")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            self._task = None

    def last_synced_at_for(self, account_id: int) -> datetime | None:
        return self._last_sync_at.get(account_id)
