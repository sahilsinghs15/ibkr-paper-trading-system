"""Persist Model Blue execution state (signal + pair position + per-leg orders) atomically."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.repositories.allocation_repository import AllocationRepository
from app.db.repositories.order_repository import OrderRepository
from app.db.repositories.signal_repository import SignalRepository
from app.db.repositories.trade_repository import TradeRepository
from app.models.model_blue_trade import OpenModelBlueTrade
from app.models.signal import Signal
from app.oms.models import OMSOrder
from app.services.model_blue.parser import ModelBlueValidationError

SessionFactory = async_sessionmaker[AsyncSession]


class ModelBlueExecutionPersistence:
    """Single-transaction writer for internal Model Blue state. IBKR remains external."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        account_id: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._account_id = account_id

    async def persist_open(
        self,
        signal: Signal,
        trade: OpenModelBlueTrade,
        orders: list[OMSOrder],
    ) -> None:
        async with self._session_factory() as session, session.begin():
            account, allocation = await self._require_account_allocation(
                session, trade.strategy_id
            )
            sig_row = await SignalRepository(session).record_processed(
                signal, persist_signal_id=trade.trade_id
            )
            await TradeRepository(session).open_trade(
                trade,
                account_id=account.id,
                target=allocation.target,
                stop=allocation.stop,
                time_limit=allocation.time_limit,
            )
            order_repo = OrderRepository(session)
            for index, order in enumerate(orders):
                await order_repo.record_oms_order(
                    order,
                    signal_pk=sig_row.id,
                    account_id=account.id,
                    trade_id=trade.trade_id,
                    strategy_id=trade.strategy_id,
                    leg_label=f"L{index}",
                )

    async def persist_close(
        self,
        signal: Signal,
        trade_id: str,
        orders: list[OMSOrder],
    ) -> OpenModelBlueTrade:
        async with self._session_factory() as session, session.begin():
            alloc_repo = AllocationRepository(session)
            account = await alloc_repo.get_enabled_account(self._account_id)
            if account is None:
                raise ModelBlueValidationError(
                    "MODEL_BLUE_ACCOUNT_MISSING: no enabled account row for persistence."
                )
            closed = await TradeRepository(session).close_trade(trade_id)
            sig_row = await SignalRepository(session).record_processed(
                signal, persist_signal_id=f"{trade_id}:CLOSE"
            )
            order_repo = OrderRepository(session)
            for index, order in enumerate(orders):
                await order_repo.record_oms_order(
                    order,
                    signal_pk=sig_row.id,
                    account_id=account.id,
                    trade_id=trade_id,
                    strategy_id=closed.strategy_id,
                    leg_label=f"L{index}",
                )
            return closed

    async def _require_account_allocation(self, session: AsyncSession, strategy_id: str):
        alloc_repo = AllocationRepository(session)
        account = await alloc_repo.get_enabled_account(self._account_id)
        if account is None:
            raise ModelBlueValidationError(
                "MODEL_BLUE_ACCOUNT_MISSING: no enabled account row for persistence."
            )
        allocation = await alloc_repo.get_allocation(
            account_id=account.id, strategy_id=strategy_id
        )
        if allocation is None:
            raise ModelBlueValidationError(
                "MODEL_BLUE_ALLOCATION_MISSING: no allocations row for "
                f"account={account.id} strategy={strategy_id}."
            )
        return account, allocation
