"""Account × Strategy configuration validation for dashboard/API use.

Does not route signals or submit orders.
"""

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.account import AccountModel
from app.db.models.strategy import AllocationModel, StrategyModel

ONE = Decimal("1")
ZERO = Decimal("0")


class AllocationConfigError(ValueError):
    """Raised when an account's strategy allocation configuration is invalid."""


class AccountStrategyConfigService:
    """Enforces allocation uniqueness, [0, 1] bounds, and enabled-pct sum ≤ 1."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def validate_account_margin(self, account: AccountModel) -> None:
        if account.total_margin is None or account.total_margin <= 0:
            raise AllocationConfigError(
                "INVALID_TOTAL_MARGIN: account.total_margin must be greater than 0."
            )

    def validate_alloc_pct(self, alloc_pct: Decimal) -> None:
        if alloc_pct < ZERO or alloc_pct > ONE:
            raise AllocationConfigError(
                f"INVALID_ALLOC_PCT: alloc_pct must be between 0 and 1 inclusive, got {alloc_pct}."
            )

    async def enabled_alloc_pct_sum(
        self, account_id: int, *, exclude_allocation_id: int | None = None
    ) -> Decimal:
        stmt = select(func.coalesce(func.sum(AllocationModel.alloc_pct), 0)).where(
            AllocationModel.account_id == account_id,
            AllocationModel.enabled.is_(True),
        )
        if exclude_allocation_id is not None:
            stmt = stmt.where(AllocationModel.id != exclude_allocation_id)
        total = (await self._session.execute(stmt)).scalar_one()
        return Decimal(str(total))

    async def ensure_unique_subscription(self, account_id: int, strategy_id: str) -> None:
        existing = (
            await self._session.execute(
                select(AllocationModel).where(
                    AllocationModel.account_id == account_id,
                    AllocationModel.strategy_id == strategy_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise AllocationConfigError(
                f"DUPLICATE_ALLOCATION: account_id={account_id} already has "
                f"strategy '{strategy_id}'."
            )

    async def ensure_strategy_exists(self, strategy_id: str) -> StrategyModel:
        strategy = (
            await self._session.execute(
                select(StrategyModel).where(StrategyModel.strategy_id == strategy_id)
            )
        ).scalar_one_or_none()
        if strategy is None:
            raise AllocationConfigError(
                f"UNKNOWN_STRATEGY: no strategies row for '{strategy_id}'."
            )
        return strategy

    async def create_allocation(
        self,
        *,
        account: AccountModel,
        strategy_id: str,
        alloc_pct: Decimal,
        target: Decimal,
        stop: Decimal,
        time_limit: int,
        enabled: bool = True,
    ) -> AllocationModel:
        await self.validate_account_margin(account)
        self.validate_alloc_pct(alloc_pct)
        await self.ensure_strategy_exists(strategy_id)
        await self.ensure_unique_subscription(account.id, strategy_id)
        if enabled:
            current = await self.enabled_alloc_pct_sum(account.id)
            if current + alloc_pct > ONE:
                raise AllocationConfigError(
                    "ALLOC_PCT_SUM_EXCEEDED: enabled allocations for this account "
                    f"would sum to {current + alloc_pct} (max 1.0)."
                )
        row = AllocationModel(
            account_id=account.id,
            strategy_id=strategy_id,
            alloc_pct=alloc_pct,
            target=target,
            stop=stop,
            time_limit=time_limit,
            enabled=enabled,
        )
        self._session.add(row)
        await self._session.flush()
        return row
