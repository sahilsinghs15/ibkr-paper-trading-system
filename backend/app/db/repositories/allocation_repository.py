"""Account-strategy allocation lookup for committed capital.

Committed notional is derived from existing columns only:

    committed = account.total_margin * allocation.alloc_pct

`alloc_pct` is a fraction of account margin (e.g. 0.50 = 50%).
This is not an invented dollar default.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.account import AccountModel
from app.db.models.strategy import AllocationModel


class AllocationRepository:
    """Read committed capital for account + strategy. No sizing math."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_enabled_account(self, account_id: int | None = None) -> AccountModel | None:
        stmt = select(AccountModel).where(AccountModel.enabled.is_(True))
        if account_id is not None:
            stmt = stmt.where(AccountModel.id == account_id)
        stmt = stmt.order_by(AccountModel.id).limit(1)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_allocation(
        self, *, account_id: int, strategy_id: str
    ) -> AllocationModel | None:
        result = await self._session.execute(
            select(AllocationModel).where(
                AllocationModel.account_id == account_id,
                AllocationModel.strategy_id == strategy_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_committed_notional(
        self, strategy_id: str, *, account_id: int | None = None
    ) -> Decimal | None:
        account = await self.get_enabled_account(account_id)
        if account is None:
            return None
        allocation = await self.get_allocation(
            account_id=account.id, strategy_id=strategy_id
        )
        if allocation is None:
            return None
        committed = account.total_margin * allocation.alloc_pct
        if committed <= 0:
            return None
        return committed
