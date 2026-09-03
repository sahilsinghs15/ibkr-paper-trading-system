"""Basket row access. No sizing or IBKR calls."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.account import AccountModel
from app.db.models.basket import BasketModel
from app.oms.basket import BasketState

INCOMPLETE_STATES = (
    BasketState.PENDING.value,
    BasketState.EXECUTING.value,
    BasketState.UNWINDING.value,
)


class BasketRepository:
    """Persist basket-level state. Child fills live on orders."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self, *, account_id: int, trade_id: str, action: str
    ) -> BasketModel | None:
        result = await self._session.execute(
            select(BasketModel).where(
                BasketModel.account_id == account_id,
                BasketModel.trade_id == trade_id,
                BasketModel.action == action,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, basket_id: int) -> BasketModel | None:
        result = await self._session.execute(
            select(BasketModel).where(BasketModel.id == basket_id)
        )
        return result.scalar_one_or_none()

    async def list_incomplete(self) -> list[BasketModel]:
        result = await self._session.execute(
            select(BasketModel).where(BasketModel.state.in_(INCOMPLETE_STATES))
        )
        return list(result.scalars().all())

    async def list_critical(self) -> list[BasketModel]:
        result = await self._session.execute(
            select(BasketModel).where(BasketModel.state == BasketState.CRITICAL.value)
        )
        return list(result.scalars().all())

    async def list_critical_for_ibkr_account(
        self, ibkr_account: str
    ) -> list[BasketModel]:
        clean = ibkr_account.strip().upper()
        result = await self._session.execute(
            select(BasketModel)
            .join(AccountModel, BasketModel.account_id == AccountModel.id)
            .where(
                BasketModel.state == BasketState.CRITICAL.value,
                func.upper(AccountModel.ibkr_account) == clean,
            )
            .order_by(BasketModel.updated_at.desc())
        )
        return list(result.scalars().all())

    async def has_critical(self, *, account_id: int, strategy_id: str) -> bool:
        result = await self._session.execute(
            select(BasketModel.id).where(
                BasketModel.account_id == account_id,
                BasketModel.strategy_id == strategy_id,
                BasketModel.state == BasketState.CRITICAL.value,
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def upsert(
        self,
        *,
        account_id: int,
        trade_id: str,
        strategy_id: str,
        action: str,
        state: str,
        intended_leg_count: int,
        recovery_status: str | None = None,
        recovery_detail: str | None = None,
        recovered_at: datetime | None = None,
    ) -> BasketModel:
        row = await self.get(account_id=account_id, trade_id=trade_id, action=action)
        if row is None:
            row = BasketModel(
                account_id=account_id,
                trade_id=trade_id,
                strategy_id=strategy_id,
                action=action,
                state=state,
                intended_leg_count=intended_leg_count,
                recovery_status=recovery_status,
                recovery_detail=recovery_detail,
                recovered_at=recovered_at,
            )
            self._session.add(row)
        else:
            row.state = state
            row.strategy_id = strategy_id
            row.intended_leg_count = intended_leg_count
            if recovery_status is not None:
                row.recovery_status = recovery_status
            if recovery_detail is not None:
                row.recovery_detail = recovery_detail
            if recovered_at is not None:
                row.recovered_at = recovered_at
        await self._session.flush()
        return row

    async def update_recovery(
        self,
        *,
        account_id: int,
        trade_id: str,
        action: str,
        recovery_status: str | None = None,
        recovery_detail: str | None = None,
        recovered_at: datetime | None = None,
        state: str | None = None,
    ) -> BasketModel | None:
        row = await self.get(account_id=account_id, trade_id=trade_id, action=action)
        if row is None:
            return None
        if recovery_status is not None:
            row.recovery_status = recovery_status
        if recovery_detail is not None:
            row.recovery_detail = recovery_detail
        if recovered_at is not None:
            row.recovered_at = recovered_at
        if state is not None:
            row.state = state
        await self._session.flush()
        return row

    async def reap_stale_executing(self, *, older_than_sec: float = 120.0) -> int:
        """Escalate aged EXECUTING baskets with no fills (M16)."""
        from app.db.models.order import OrderModel

        cutoff = datetime.now(UTC) - timedelta(seconds=older_than_sec)
        result = await self._session.execute(
            select(BasketModel).where(
                BasketModel.state == BasketState.EXECUTING.value,
                BasketModel.updated_at < cutoff,
            )
        )
        escalated = 0
        for row in list(result.scalars().all()):
            fills = await self._session.execute(
                select(func.coalesce(func.sum(OrderModel.fill_qty), 0)).where(
                    OrderModel.basket_id == row.id
                )
            )
            total_filled = fills.scalar_one()
            if total_filled and float(total_filled) > 0:
                continue
            row.state = BasketState.CRITICAL.value
            row.recovery_status = "REAPED"
            row.recovery_detail = (
                f"Aged EXECUTING with no fills for {older_than_sec:.0f}s"
            )
            escalated += 1
        if escalated:
            await self._session.flush()
        return escalated
