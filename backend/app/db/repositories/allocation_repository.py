"""Account-strategy allocation lookup for committed capital.

Committed notional is derived from existing columns only:

    committed = account.total_margin * allocation.alloc_pct

`alloc_pct` is a fraction of account margin (e.g. 0.50 = 50%).
This is not an invented dollar default.

Routing uses DatabaseStrategyAccountRouter. This repository does not
select a default/first account when account_id is omitted.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.account import AccountModel
from app.db.models.strategy import AllocationModel, StrategyModel


class AllocationRepository:
    """Read committed capital for account + strategy. No sizing math."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_account(self, account_id: int) -> AccountModel | None:
        result = await self._session.execute(
            select(AccountModel).where(AccountModel.id == account_id)
        )
        return result.scalar_one_or_none()

    async def get_enabled_account(self, account_id: int | None = None) -> AccountModel | None:
        """Return the named enabled account. Does not pick an arbitrary first row."""
        if account_id is None:
            return None
        result = await self._session.execute(
            select(AccountModel).where(
                AccountModel.id == account_id,
                AccountModel.enabled.is_(True),
            )
        )
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
        if account_id is None:
            return None
        account = await self.get_enabled_account(account_id)
        if account is None or account.total_margin <= 0:
            return None
        strategy = (
            await self._session.execute(
                select(StrategyModel).where(
                    StrategyModel.strategy_id == strategy_id,
                    StrategyModel.enabled.is_(True),
                )
            )
        ).scalar_one_or_none()
        if strategy is None:
            return None
        allocation = await self.get_allocation(
            account_id=account.id, strategy_id=strategy_id
        )
        if allocation is None or not allocation.enabled:
            return None
        committed = account.total_margin * allocation.alloc_pct
        if committed <= 0:
            return None
        return committed
