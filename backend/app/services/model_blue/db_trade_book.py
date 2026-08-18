"""PostgreSQL-backed Model Blue trade book (survives process restart)."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.repositories.allocation_repository import AllocationRepository
from app.db.repositories.trade_repository import TradeRepository
from app.models.model_blue_trade import OpenModelBlueTrade
from app.services.model_blue.parser import ModelBlueValidationError

SessionFactory = async_sessionmaker[AsyncSession]


class DatabaseModelBlueTradeBook:
    """Trade book backed by the pair-level `positions` table."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        account_id: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._account_id = account_id

    async def get(self, trade_id: str) -> OpenModelBlueTrade | None:
        async with self._session_factory() as session:
            return await TradeRepository(session).get_open(trade_id)

    async def record_open(self, trade: OpenModelBlueTrade) -> None:
        async with self._session_factory() as session, session.begin():
            alloc_repo = AllocationRepository(session)
            account = await alloc_repo.get_enabled_account(self._account_id)
            if account is None:
                raise ModelBlueValidationError(
                    "MODEL_BLUE_ACCOUNT_MISSING: no enabled account row for position persistence."
                )
            allocation = await alloc_repo.get_allocation(
                account_id=account.id, strategy_id=trade.strategy_id
            )
            if allocation is None:
                raise ModelBlueValidationError(
                    "MODEL_BLUE_ALLOCATION_MISSING: cannot persist OPEN without "
                    f"an allocations row for account={account.id} strategy={trade.strategy_id}."
                )
            await TradeRepository(session).open_trade(
                trade,
                account_id=account.id,
                target=allocation.target,
                stop=allocation.stop,
                time_limit=allocation.time_limit,
            )

    async def close(self, trade_id: str) -> OpenModelBlueTrade:
        async with self._session_factory() as session, session.begin():
            return await TradeRepository(session).close_trade(trade_id)
