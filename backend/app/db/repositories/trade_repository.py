"""Trade-level access for Model Blue two-leg spreads.

A trade is stored as one `positions` row keyed by trade_id (leg A + leg B).
"""

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.position import PositionModel
from app.db.repositories.position_repository import PositionRepository
from app.models.model_blue_trade import OpenModelBlueTrade


class TradeRepository:
    """Preserves the two-leg relationship of a Model Blue trade."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._positions = PositionRepository(session)

    async def get_open(self, trade_id: str) -> OpenModelBlueTrade | None:
        return await self._positions.get_open_trade(trade_id)

    async def get_row(self, trade_id: str) -> PositionModel | None:
        return await self._positions.get_by_trade_id(trade_id)

    async def open_trade(
        self,
        trade: OpenModelBlueTrade,
        *,
        account_id: int,
        target: Decimal,
        stop: Decimal,
        time_limit: int,
    ) -> PositionModel:
        return await self._positions.open_trade(
            trade,
            account_id=account_id,
            target=target,
            stop=stop,
            time_limit=time_limit,
        )

    async def close_trade(self, trade_id: str) -> OpenModelBlueTrade:
        row = await self._positions.close_trade(trade_id)
        return self._positions.to_open_trade(row)
