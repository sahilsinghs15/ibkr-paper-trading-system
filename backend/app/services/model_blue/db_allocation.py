"""Database-backed committed capital for ModelBlueSizer.

Authoritative source once wired. Missing allocation → None (sizer/OrderManager reject).
Does not invent a dollar amount and does not read MODEL_BLUE_COMMITTED_NOTIONAL.
"""

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.repositories.allocation_repository import AllocationRepository
from app.services.model_blue.parser import is_model_blue_strategy

SessionFactory = async_sessionmaker[AsyncSession]


class DatabaseCommittedCapitalProvider:
    """Async committed-capital provider reading PostgreSQL allocations."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        account_id: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._account_id = account_id

    async def get_committed(
        self, strategy_id: str, account_id: int | None = None
    ) -> Decimal | None:
        if not is_model_blue_strategy(strategy_id):
            return None
        resolved = account_id if account_id is not None else self._account_id
        if resolved is None:
            return None
        async with self._session_factory() as session:
            return await AllocationRepository(session).get_committed_notional(
                strategy_id, account_id=resolved
            )
