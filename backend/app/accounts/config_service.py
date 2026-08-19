"""Account × Strategy configuration validation for dashboard/API use.

Does not route signals or submit orders.
"""

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.account import AccountModel, PerSymbolLimitModel
from app.db.models.execution_settings import ExecutionSettingsModel
from app.db.models.strategy import AllocationModel, StrategyModel
from app.oms.retry_policy import ExecutionRetryPolicy

ONE = Decimal(1)
ZERO = Decimal(0)


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

    async def create_account(
        self,
        *,
        name: str,
        ibkr_account: str,
        total_margin: Decimal,
        enabled: bool = True,
    ) -> AccountModel:
        clean_name = name.strip()
        clean_ibkr = ibkr_account.strip().upper()
        if not clean_name:
            raise AllocationConfigError("INVALID_NAME: Account name required.")
        if not clean_ibkr:
            raise AllocationConfigError("INVALID_IBKR_ACCOUNT: IBKR account identifier required.")
        if total_margin <= ZERO:
            raise AllocationConfigError("INVALID_TOTAL_MARGIN: total_margin must be greater than 0.")

        existing = (
            await self._session.execute(
                select(AccountModel).where(func.upper(AccountModel.ibkr_account) == clean_ibkr)
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise AllocationConfigError(
                f"DUPLICATE_IBKR_ACCOUNT: An account with IBKR identifier '{clean_ibkr}' already exists."
            )

        row = AccountModel(
            name=clean_name,
            ibkr_account=clean_ibkr,
            total_margin=total_margin,
            enabled=enabled,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def has_trading_history(self, account_id: int) -> bool:
        from app.db.models.basket import BasketModel
        from app.db.models.execution import ExecutionModel
        from app.db.models.order import OrderModel
        from app.db.models.position import PositionModel

        for model in (OrderModel, ExecutionModel, PositionModel, BasketModel):
            cnt = (
                await self._session.execute(
                    select(func.count()).select_from(model).where(model.account_id == account_id)
                )
            ).scalar_one()
            if cnt > 0:
                return True
        return False

    async def check_account_deletable(self, account_id: int) -> tuple[bool, str | None]:
        account = await self.get_account(account_id)
        if account is None:
            return False, f"UNKNOWN_ACCOUNT: Account {account_id} not found."
        history = await self.has_trading_history(account_id)
        if history:
            return (
                False,
                "Account deletion is unavailable because this account has trading history. Disable the account instead.",
            )
        return True, None

    async def delete_account(self, account_id: int) -> None:
        can_del, reason = await self.check_account_deletable(account_id)
        if not can_del:
            raise AllocationConfigError(reason or "Cannot delete account.")
        account = await self.get_account(account_id)
        if account is None:
            return

        allocations = (
            await self._session.execute(
                select(AllocationModel).where(AllocationModel.account_id == account_id)
            )
        ).scalars().all()
        for alloc in allocations:
            await self._session.delete(alloc)

        limits = (
            await self._session.execute(
                select(PerSymbolLimitModel).where(PerSymbolLimitModel.account_id == account_id)
            )
        ).scalars().all()
        for lim in limits:
            await self._session.delete(lim)

        await self._session.delete(account)
        await self._session.flush()

    async def update_account(
        self,
        account: AccountModel,
        *,
        name: str | None = None,
        ibkr_account: str | None = None,
        total_margin: Decimal | None = None,
        enabled: bool | None = None,
    ) -> AccountModel:
        if name is not None:
            clean_name = name.strip()
            if not clean_name:
                raise AllocationConfigError("INVALID_NAME: Account name required.")
            account.name = clean_name
        if ibkr_account is not None:
            clean_ibkr = ibkr_account.strip().upper()
            if not clean_ibkr:
                raise AllocationConfigError("INVALID_IBKR_ACCOUNT: IBKR account identifier required.")
            if clean_ibkr != account.ibkr_account.upper():
                history = await self.has_trading_history(account.id)
                if history:
                    raise AllocationConfigError(
                        "IBKR account identifier cannot be changed because this account has trading history."
                    )
                existing = (
                    await self._session.execute(
                        select(AccountModel).where(
                            func.upper(AccountModel.ibkr_account) == clean_ibkr,
                            AccountModel.id != account.id,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    raise AllocationConfigError(
                        f"DUPLICATE_IBKR_ACCOUNT: An account with IBKR identifier '{clean_ibkr}' already exists."
                    )
                account.ibkr_account = clean_ibkr
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

    async def get_or_create_execution_settings(self) -> ExecutionSettingsModel:
        row = (
            await self._session.execute(
                select(ExecutionSettingsModel).where(ExecutionSettingsModel.id == 1)
            )
        ).scalar_one_or_none()
        if row is not None:
            return row
        row = ExecutionSettingsModel(
            id=1,
            enabled=True,
            square_off_after_sec=30,
            max_retries=3,
            retry_interval_sec=5,
            retry_window_sec=30,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def update_execution_settings(
        self,
        *,
        enabled: bool | None = None,
        square_off_after_sec: int | None = None,
        max_retries: int | None = None,
        retry_interval_sec: int | None = None,
        retry_window_sec: int | None = None,
    ) -> ExecutionSettingsModel:
        row = await self.get_or_create_execution_settings()
        if enabled is not None:
            row.enabled = enabled
        if square_off_after_sec is not None:
            row.square_off_after_sec = square_off_after_sec
        if max_retries is not None:
            row.max_retries = max_retries
        if retry_interval_sec is not None:
            row.retry_interval_sec = retry_interval_sec
        if retry_window_sec is not None:
            row.retry_window_sec = retry_window_sec
        try:
            ExecutionRetryPolicy(
                enabled=row.enabled,
                square_off_after_sec=float(row.square_off_after_sec),
                max_retries=int(row.max_retries),
                retry_interval_sec=float(row.retry_interval_sec),
                retry_window_sec=float(row.retry_window_sec),
            ).validate()
        except ValueError as exc:
            raise AllocationConfigError(str(exc)) from exc
        await self._session.flush()
        return row

