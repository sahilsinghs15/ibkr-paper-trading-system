"""Persistence for pair-level Model Blue positions (two legs on one trade_id row)."""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identifiers import normalize_symbol
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

    async def list_open(self) -> list[PositionModel]:
        result = await self._session.execute(
            select(PositionModel).where(PositionModel.risk_state == RISK_STATE_OPEN)
        )
        return list(result.scalars().all())

    async def list_all(self) -> list[PositionModel]:
        result = await self._session.execute(select(PositionModel))
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
        if existing is not None:
            raise ValueError(
                f"TRADE_ID_NOT_UNIQUE: trade_id '{trade.trade_id}' already exists "
                f"for account_id={account_id} (risk_state={existing.risk_state}); "
                "refusing to mutate."
            )

        leg_a, leg_b = trade.legs[0], trade.legs[1]
        row = PositionModel(
            trade_id=trade.trade_id,
            strategy_id=trade.strategy_id,
            account_id=account_id,
            leg_a_symbol=normalize_symbol(leg_a.symbol),
            leg_a_signed_qty=_signed_qty(leg_a.side, leg_a.quantity),
            leg_a_entry_mark=leg_a.price,
            leg_b_symbol=normalize_symbol(leg_b.symbol),
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
        needed: list[str] = []
        if row.leg_a_symbol:
            needed.append(row.leg_a_symbol)
        if row.leg_b_symbol:
            needed.append(row.leg_b_symbol)
        if exit_marks is not None:
            missing = [symbol for symbol in needed if symbol not in exit_marks]
            if missing:
                raise ValueError(
                    f"INCOMPLETE_EXIT_MARKS: trade_id={trade_id} missing {missing}"
                )
        marks = exit_marks or {}
        realised = Decimal(0)
        if marks:
            if row.leg_a_symbol in marks and row.leg_a_entry_mark is not None:
                realised += row.leg_a_signed_qty * (
                    marks[row.leg_a_symbol] - row.leg_a_entry_mark
                )
            if (
                row.leg_b_symbol
                and row.leg_b_symbol in marks
                and row.leg_b_entry_mark is not None
                and row.leg_b_signed_qty is not None
            ):
                realised += row.leg_b_signed_qty * (
                    marks[row.leg_b_symbol] - row.leg_b_entry_mark
                )
        row.realised_pnl = realised
        if commission is not None and commission > 0:
            prior = row.commission or Decimal(0)
            row.commission = prior + commission
            realised = realised - row.commission
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
