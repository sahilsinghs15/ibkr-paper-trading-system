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
    symbol: str | None,
    signed_qty: Decimal | None,
    price: Decimal | None,
    instrument_type: str | None,
) -> OpenModelBlueTradeLeg | None:
    if not symbol or signed_qty is None or price is None:
        return None
    side = OrderSide.BUY if signed_qty >= 0 else OrderSide.SELL
    return OpenModelBlueTradeLeg(
        symbol=symbol,
        instrument_type=instrument_type or "STK",
        side=side,
        quantity=abs(signed_qty),
        price=price,
    )


class PositionRepository:
    """Open/close pair positions. No quantity calculation."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_trade_id(
        self, trade_id: str, *, account_id: int | None = None
    ) -> PositionModel | None:
        stmt = select(PositionModel).where(PositionModel.trade_id == trade_id)
        if account_id is not None:
            stmt = stmt.where(PositionModel.account_id == account_id)
        result = await self._session.execute(stmt)
        rows = list(result.scalars().all())
        if len(rows) > 1:
            raise ValueError(
                f"AMBIGUOUS_TRADE_ID: trade_id '{trade_id}' exists on multiple accounts; "
                "account_id is required."
            )
        return rows[0] if rows else None

    async def get_open_by_trade_id(
        self, trade_id: str, *, account_id: int | None = None
    ) -> PositionModel | None:
        stmt = select(PositionModel).where(
            PositionModel.trade_id == trade_id,
            PositionModel.risk_state == RISK_STATE_OPEN,
        )
        if account_id is not None:
            stmt = stmt.where(PositionModel.account_id == account_id)
        result = await self._session.execute(stmt)
        rows = list(result.scalars().all())
        if len(rows) > 1:
            raise ValueError(
                f"AMBIGUOUS_TRADE_ID: open trade_id '{trade_id}' exists on multiple accounts; "
                "account_id is required."
            )
        return rows[0] if rows else None

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

    async def get_open_trade(
        self, trade_id: str, *, account_id: int | None = None
    ) -> OpenModelBlueTrade | None:
        row = await self.get_open_by_trade_id(trade_id, account_id=account_id)
        if row is None:
            return None
        return self.to_open_trade(row)

    def to_open_trade(self, row: PositionModel) -> OpenModelBlueTrade:
        legs: list[OpenModelBlueTradeLeg] = []
        leg_a = _leg_from_signed(
            row.leg_a_symbol,
            row.leg_a_signed_qty,
            row.leg_a_entry_mark,
            getattr(row, "leg_a_instrument_type", None),
        )
        if leg_a is not None:
            legs.append(leg_a)
        leg_b = _leg_from_signed(
            row.leg_b_symbol,
            row.leg_b_signed_qty,
            row.leg_b_entry_mark,
            getattr(row, "leg_b_instrument_type", None),
        )
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
        existing = await self.get_by_trade_id(trade.trade_id, account_id=account_id)
        if existing is not None and existing.risk_state == RISK_STATE_OPEN:
            raise ValueError(
                f"Open position already exists for trade_id '{trade.trade_id}' "
                f"account_id={account_id}."
            )

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
                leg_a_instrument_type=leg_a.instrument_type,
                leg_b_instrument_type=leg_b.instrument_type,
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
        existing.leg_a_instrument_type = leg_a.instrument_type
        existing.leg_b_instrument_type = leg_b.instrument_type
        existing.target = target
        existing.stop = stop
        existing.time_limit = time_limit
        existing.risk_state = RISK_STATE_OPEN
        existing.closed_at = None
        await self._session.flush()
        return existing

    async def close_trade(
        self,
        trade_id: str,
        *,
        account_id: int | None = None,
        exit_marks: dict[str, Decimal] | None = None,
        commission: Decimal | None = None,
    ) -> PositionModel:
        row = await self.get_open_by_trade_id(trade_id, account_id=account_id)
        if row is None:
            raise KeyError(trade_id)
        realised = Decimal(0)
        if exit_marks:
            if row.leg_a_symbol in exit_marks and row.leg_a_entry_mark is not None:
                realised += row.leg_a_signed_qty * (
                    exit_marks[row.leg_a_symbol] - row.leg_a_entry_mark
                )
            if (
                row.leg_b_symbol
                and row.leg_b_symbol in exit_marks
                and row.leg_b_entry_mark is not None
                and row.leg_b_signed_qty is not None
            ):
                realised += row.leg_b_signed_qty * (
                    exit_marks[row.leg_b_symbol] - row.leg_b_entry_mark
                )
        row.realised_pnl = realised
        if commission is not None and commission > 0:
            row.commission = commission
            realised = realised - commission
            row.realised_pnl = realised
        row.live_pnl = realised
        row.risk_state = RISK_STATE_CLOSED
        row.closed_at = datetime.now(UTC)
        await self._session.flush()
        return row

    async def update_live_pnl(
        self, *, account_id: int, trade_id: str, live_pnl: Decimal
    ) -> None:
        row = await self.get_open_by_trade_id(trade_id, account_id=account_id)
        if row is None:
            return
        row.live_pnl = live_pnl
        await self._session.flush()
