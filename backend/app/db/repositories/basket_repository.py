"""Basket row access. No sizing or IBKR calls."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
            )
            self._session.add(row)
        else:
            row.state = state
            row.strategy_id = strategy_id
            row.intended_leg_count = intended_leg_count
        await self._session.flush()
        return row
