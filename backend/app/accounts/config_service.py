"""Account × Strategy configuration validation for dashboard/API use.

Does not route signals or submit orders.
"""

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.account import AccountModel, PerSymbolLimitModel
from app.db.models.strategy import AllocationModel, StrategyModel

ONE = Decimal("1")
ZERO = Decimal("0")


class AllocationConfigError(ValueError):
    """Raised when an account's strategy allocation configuration is invalid."""


class AccountStrategyConfigService:
    """Enforces allocation uniqueness, [0, 1] bounds, and enabled-pct sum ≤ 1."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_account(self, account_id: int) -> AccountModel | None:
        result = await self._session.execute(
            select(AccountModel).where(AccountModel.id == account_id)
        )
        return result.scalar_one_or_none()

    async def get_allocation(self, allocation_id: int) -> AllocationModel | None:
        result = await self._session.execute(
            select(AllocationModel).where(AllocationModel.id == allocation_id)
        )
        return result.scalar_one_or_none()

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

    def validate_max_open_positions(self, max_open_positions: int) -> None:
        if max_open_positions < 0:
            raise AllocationConfigError(
                "INVALID_MAX_OPEN_POSITIONS: max_open_positions must be >= 0."
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

    async def _validate_enabled_sum(
        self,
        account_id: int,
        *,
        alloc_pct: Decimal,
        enabled: bool,
        exclude_allocation_id: int | None = None,
    ) -> None:
        if not enabled:
            return
        current = await self.enabled_alloc_pct_sum(
            account_id, exclude_allocation_id=exclude_allocation_id
        )
        if current + alloc_pct > ONE:
            raise AllocationConfigError(
                "ALLOC_PCT_SUM_EXCEEDED: enabled allocations for this account "
                f"would sum to {current + alloc_pct} (max 1.0)."
            )

    async def update_account(
        self,
        account: AccountModel,
        *,
        total_margin: Decimal | None = None,
        enabled: bool | None = None,
    ) -> AccountModel:
        if total_margin is not None:
            account.total_margin = total_margin
            await self.validate_account_margin(account)
        if enabled is not None:
            account.enabled = enabled
        await self._session.flush()
        return account

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
        max_open_positions: int | None = None,
    ) -> AllocationModel:
        await self.validate_account_margin(account)
        self.validate_alloc_pct(alloc_pct)
        strategy = await self.ensure_strategy_exists(strategy_id)
        await self.ensure_unique_subscription(account.id, strategy_id)
        cap = (
            max_open_positions
            if max_open_positions is not None
            else strategy.max_open_positions
        )
        self.validate_max_open_positions(cap)
        await self._validate_enabled_sum(
            account.id, alloc_pct=alloc_pct, enabled=enabled
        )
        row = AllocationModel(
            account_id=account.id,
            strategy_id=strategy_id,
            alloc_pct=alloc_pct,
            target=target,
            stop=stop,
            time_limit=time_limit,
            max_open_positions=cap,
            enabled=enabled,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def update_allocation(
        self,
        allocation: AllocationModel,
        *,
        alloc_pct: Decimal | None = None,
        enabled: bool | None = None,
        max_open_positions: int | None = None,
    ) -> AllocationModel:
        new_pct = allocation.alloc_pct if alloc_pct is None else alloc_pct
        new_enabled = allocation.enabled if enabled is None else enabled
        if alloc_pct is not None:
            self.validate_alloc_pct(alloc_pct)
            allocation.alloc_pct = alloc_pct
        if enabled is not None:
            allocation.enabled = enabled
        if max_open_positions is not None:
            self.validate_max_open_positions(max_open_positions)
            allocation.max_open_positions = max_open_positions
        await self._validate_enabled_sum(
            allocation.account_id,
            alloc_pct=new_pct,
            enabled=new_enabled,
            exclude_allocation_id=allocation.id,
        )
        await self._session.flush()
        return allocation

    async def upsert_symbol_limit(
        self,
        *,
        account_id: int,
        symbol: str,
        money_limit: Decimal,
    ) -> PerSymbolLimitModel:
        account = await self.get_account(account_id)
        if account is None:
            raise AllocationConfigError(f"UNKNOWN_ACCOUNT: no account id={account_id}.")
        if money_limit <= ZERO:
            raise AllocationConfigError(
                "INVALID_MONEY_LIMIT: money_limit must be greater than 0."
            )
        sym = symbol.strip().upper()
        existing = (
            await self._session.execute(
                select(PerSymbolLimitModel).where(
                    PerSymbolLimitModel.account_id == account_id,
                    PerSymbolLimitModel.symbol == sym,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.money_limit = money_limit
            await self._session.flush()
            return existing
        row = PerSymbolLimitModel(
            account_id=account_id,
            symbol=sym,
            money_limit=money_limit,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def delete_symbol_limit(self, *, account_id: int, symbol: str) -> bool:
        sym = symbol.strip().upper()
        existing = (
            await self._session.execute(
                select(PerSymbolLimitModel).where(
                    PerSymbolLimitModel.account_id == account_id,
                    PerSymbolLimitModel.symbol == sym,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            return False
        await self._session.delete(existing)
        await self._session.flush()
        return True
