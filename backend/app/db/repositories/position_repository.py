"""Persistence for pair-level Model Blue positions (two legs on one trade_id row)."""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.position import PositionModel
from app.models.model_blue_trade import OpenModelBlueTrade, OpenModelBlueTradeLeg
from app.rms.models import OrderSide

RISK_STATE_OPEN = "OPEN"
RISK_STATE_CLOSED = "CLOSED"


def _signed_qty(side: OrderSide, quantity: Decimal) -> Decimal:
    qty = Decimal(str(quantity))
    return qty if side == OrderSide.BUY else -qty


def _leg_from_signed(
    symbol: str | None, signed_qty: Decimal | None, price: Decimal | None
) -> OpenModelBlueTradeLeg | None:
    if not symbol or signed_qty is None or price is None:
        return None
    side = OrderSide.BUY if signed_qty >= 0 else OrderSide.SELL
    return OpenModelBlueTradeLeg(
        symbol=symbol,
        instrument_type="STK",
        side=side,
        quantity=abs(signed_qty),
        price=price,
    )


class PositionRepository:
    """Open/close pair positions. No quantity calculation."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_trade_id(self, trade_id: str) -> PositionModel | None:
        result = await self._session.execute(
            select(PositionModel).where(PositionModel.trade_id == trade_id)
        )
        return result.scalar_one_or_none()

    async def get_open_by_trade_id(self, trade_id: str) -> PositionModel | None:
        result = await self._session.execute(
            select(PositionModel).where(
                PositionModel.trade_id == trade_id,
                PositionModel.risk_state == RISK_STATE_OPEN,
            )
        )
        return result.scalar_one_or_none()

    async def get_open_by_strategy_symbol(
        self, strategy_id: str, symbol: str
    ) -> list[PositionModel]:
        result = await self._session.execute(
            select(PositionModel).where(
                PositionModel.strategy_id == strategy_id,
                PositionModel.risk_state == RISK_STATE_OPEN,
                or_(
                    PositionModel.leg_a_symbol == symbol,
                    PositionModel.leg_b_symbol == symbol,
                ),
            )
        )
        return list(result.scalars().all())

    async def list_open(self) -> list[PositionModel]:
        result = await self._session.execute(
            select(PositionModel).where(PositionModel.risk_state == RISK_STATE_OPEN)
        )
        return list(result.scalars().all())

    async def get_open_trade(self, trade_id: str) -> OpenModelBlueTrade | None:
        row = await self.get_open_by_trade_id(trade_id)
        if row is None:
            return None
        return self.to_open_trade(row)

    def to_open_trade(self, row: PositionModel) -> OpenModelBlueTrade:
        legs: list[OpenModelBlueTradeLeg] = []
        leg_a = _leg_from_signed(row.leg_a_symbol, row.leg_a_signed_qty, row.leg_a_entry_mark)
        if leg_a is not None:
            legs.append(leg_a)
        leg_b = _leg_from_signed(row.leg_b_symbol, row.leg_b_signed_qty, row.leg_b_entry_mark)
        if leg_b is not None:
            legs.append(leg_b)
        return OpenModelBlueTrade(
            trade_id=row.trade_id,
            strategy_id=row.strategy_id,
            direction=0,
            legs=tuple(legs),
        )

    async def open_trade(
        self,
        trade: OpenModelBlueTrade,
        *,
        account_id: int,
        target: Decimal,
        stop: Decimal,
        time_limit: int,
    ) -> PositionModel:
        # Pair-row schema (DB-3 deferred): Model Blue persistence, not generic N-leg storage.
        if len(trade.legs) != 2:
            raise ValueError("Model Blue position requires exactly two legs.")
        existing = await self.get_by_trade_id(trade.trade_id)
        if existing is not None and existing.risk_state == RISK_STATE_OPEN:
            raise ValueError(f"Open position already exists for trade_id '{trade.trade_id}'.")

        leg_a, leg_b = trade.legs[0], trade.legs[1]
        if existing is None:
            row = PositionModel(
                trade_id=trade.trade_id,
                strategy_id=trade.strategy_id,
                account_id=account_id,
                leg_a_symbol=leg_a.symbol,
                leg_a_signed_qty=_signed_qty(leg_a.side, leg_a.quantity),
                leg_a_entry_mark=leg_a.price,
                leg_b_symbol=leg_b.symbol,
                leg_b_signed_qty=_signed_qty(leg_b.side, leg_b.quantity),
                leg_b_entry_mark=leg_b.price,
                target=target,
                stop=stop,
                time_limit=time_limit,
                risk_state=RISK_STATE_OPEN,
            )
            self._session.add(row)
            await self._session.flush()
            return row

        existing.strategy_id = trade.strategy_id
        existing.account_id = account_id
        existing.leg_a_symbol = leg_a.symbol
        existing.leg_a_signed_qty = _signed_qty(leg_a.side, leg_a.quantity)
        existing.leg_a_entry_mark = leg_a.price
        existing.leg_b_symbol = leg_b.symbol
        existing.leg_b_signed_qty = _signed_qty(leg_b.side, leg_b.quantity)
        existing.leg_b_entry_mark = leg_b.price
        existing.target = target
        existing.stop = stop
        existing.time_limit = time_limit
        existing.risk_state = RISK_STATE_OPEN
        existing.closed_at = None
        await self._session.flush()
        return existing

    async def close_trade(self, trade_id: str) -> PositionModel:
        row = await self.get_open_by_trade_id(trade_id)
        if row is None:
            raise KeyError(trade_id)
        row.risk_state = RISK_STATE_CLOSED
        row.closed_at = datetime.now(UTC)
        await self._session.flush()
        return row
