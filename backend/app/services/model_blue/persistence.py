"""Persist Model Blue execution state (signal + pair position + per-leg orders) atomically."""

import logging
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.repositories.allocation_repository import AllocationRepository
from app.db.repositories.event_repository import EventRepository
from app.db.repositories.order_repository import OrderRepository
from app.db.repositories.signal_repository import SignalRepository
from app.db.repositories.trade_repository import TradeRepository
from app.models.model_blue_trade import OpenModelBlueTrade, OpenModelBlueTradeLeg
from app.models.signal import Signal
from app.oms.models import (
    OMSOrder,
    OMSOrderStatus,
    executions_commission_total,
    executions_weighted_average,
)
from app.services.model_blue.parser import ModelBlueValidationError

logger = logging.getLogger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]


def _decimal_or_none(raw: object) -> Decimal | None:
    if raw is None:
        return None
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _exit_marks_from_orders(orders: list[OMSOrder]) -> dict[str, Decimal]:
    marks: dict[str, Decimal] = {}
    for order in orders:
        if getattr(order, "is_compensation", False):
            continue
        derived = executions_weighted_average(getattr(order, "executions", {}) or {})
        raw = derived or order.average_fill_price or order.last_fill_price
        price = _decimal_or_none(raw)
        if price is None or not order.symbol:
            continue
        marks[order.symbol] = price
    return marks


def _commission_from_orders(orders: list[OMSOrder]) -> Decimal | None:
    total = Decimal(0)
    found = False
    for order in orders:
        execs = getattr(order, "executions", None) or {}
        if execs:
            part = executions_commission_total(execs)
            if part > 0:
                found = True
                total += part
            continue
        raw = getattr(order, "commission", None)
        if raw is None:
            continue
        found = True
        total += raw if isinstance(raw, Decimal) else Decimal(str(raw))
    return total if found and total > 0 else None


_CLOSE_QTY_EPS = Decimal("0.0001")


def _filled_qty_by_symbol(orders: list[OMSOrder]) -> dict[str, Decimal]:
    filled: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    for order in orders:
        if getattr(order, "is_compensation", False):
            continue
        qty = _decimal_or_none(
            getattr(order, "filled_quantity", None)
            or getattr(order, "fill_qty", None)
            or getattr(order, "quantity", None)
        )
        if qty is None or qty <= 0 or not order.symbol:
            continue
        filled[order.symbol] += qty
    return dict(filled)


def assert_close_qty_matches_open(
    *,
    trade_id: str,
    leg_a_symbol: str | None,
    leg_a_signed_qty: Decimal | None,
    leg_b_symbol: str | None,
    leg_b_signed_qty: Decimal | None,
    filled_by_symbol: dict[str, Decimal],
) -> None:
    """Refuse CLOSE that would mark the full open size while fills are short (M40)."""
    for symbol, signed in (
        (leg_a_symbol, leg_a_signed_qty),
        (leg_b_symbol, leg_b_signed_qty),
    ):
        if not symbol or signed is None:
            continue
        open_qty = abs(signed)
        close_qty = filled_by_symbol.get(symbol, Decimal(0))
        if abs(close_qty - open_qty) > _CLOSE_QTY_EPS:
            raise ModelBlueValidationError(
                f"CLOSE_QTY_MISMATCH: trade_id={trade_id} symbol={symbol} "
                f"close_filled={close_qty} open_qty={open_qty}"
            )


def _open_trade_from_fills(
    trade: OpenModelBlueTrade, orders: list[OMSOrder]
) -> OpenModelBlueTrade:
    """Build the pair row from actual FILLED broker quantities and avg prices."""
    fill_orders = [o for o in orders if not getattr(o, "is_compensation", False)]
    by_leg: dict[int, list[OMSOrder]] = defaultdict(list)
    for order in fill_orders:
        idx = order.leg_index if order.leg_index is not None else 0
        by_leg[idx].append(order)
    if len(by_leg) != 2:
        raise ModelBlueValidationError(
            "POSITION_REQUIRES_FILLS: Model Blue OPEN persists only after both "
            f"legs are filled (got {len(by_leg)} leg_index groups)."
        )
    legs: list[OpenModelBlueTradeLeg] = []
    for index in sorted(by_leg):
        group = by_leg[index]
        qty = Decimal(0)
        weighted = Decimal(0)
        instrument_type = None
        symbol = None
        side = None
        for order in group:
            if order.status != OMSOrderStatus.FILLED and float(order.filled_quantity or 0) <= 0:
                continue
            fill_qty = Decimal(str(order.filled_quantity))
            if fill_qty <= 0:
                continue
            derived = executions_weighted_average(getattr(order, "executions", {}) or {})
            raw = derived or order.average_fill_price or order.last_fill_price
            if raw is None:
                raise ModelBlueValidationError(
                    f"POSITION_REQUIRES_FILLS: {order.symbol} has no fill price."
                )
            price = raw if isinstance(raw, Decimal) else Decimal(str(raw))
            qty += fill_qty
            weighted += fill_qty * price
            symbol = order.symbol
            side = order.side
            resolved = getattr(order, "resolved", None)
            if resolved is not None:
                instrument_type = resolved.sec_type
            elif (
                order.leg_index is not None
                and order.intent.legs
                and 0 <= order.leg_index < len(order.intent.legs)
            ):
                instrument_type = order.intent.legs[order.leg_index].instrument_type
        if qty <= 0 or symbol is None or side is None:
            raise ModelBlueValidationError(
                f"POSITION_REQUIRES_FILLS: leg_index={index} has no filled quantity."
            )
        if not instrument_type:
            raise ModelBlueValidationError(
                f"POSITION_REQUIRES_INSTRUMENT_TYPE: {symbol} fill has no instrument_type."
            )
        price = weighted / qty
        legs.append(
            OpenModelBlueTradeLeg(
                symbol=symbol,
                instrument_type=instrument_type,
                side=side,
                quantity=qty,
                price=price,
            )
        )
    return OpenModelBlueTrade(
        trade_id=trade.trade_id,
        strategy_id=trade.strategy_id,
        direction=trade.direction,
        legs=tuple(legs),
    )


class ModelBlueExecutionPersistence:
    """Single-transaction writer for internal Model Blue state. IBKR remains external."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        account_id: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._account_id = account_id

    def _account_id_from(
        self, orders: list[OMSOrder], account_id: int | None
    ) -> int:
        if account_id is not None:
            return account_id
        if orders and orders[0].intent.account_id is not None:
            return orders[0].intent.account_id
        if self._account_id is not None:
            return self._account_id
        raise ModelBlueValidationError(
            "MODEL_BLUE_ACCOUNT_MISSING: persistence requires an explicit account_id."
        )

    async def persist_open(
        self,
        signal: Signal,
        trade: OpenModelBlueTrade,
        orders: list[OMSOrder],
        *,
        account_id: int | None = None,
    ) -> None:
        resolved = self._account_id_from(orders, account_id)
        trade = _open_trade_from_fills(trade, orders)
        async with self._session_factory() as session, session.begin():
            account, allocation = await self._require_account_allocation(
                session, trade.strategy_id, resolved
            )
            sig_row = await SignalRepository(session).record_processed(
                signal, persist_signal_id=trade.trade_id
            )
            row = await TradeRepository(session).open_trade(
                trade,
                account_id=account.id,
                target=allocation.target,
                stop=allocation.stop,
                time_limit=allocation.time_limit,
            )
            comm = _commission_from_orders(orders)
            if comm is not None and comm > 0:
                row.commission = comm
            order_repo = OrderRepository(session)
            for index, order in enumerate(orders):
                if getattr(order, "is_compensation", False):
                    continue
                await order_repo.record_oms_order(
                    order,
                    signal_pk=sig_row.id,
                    account_id=account.id,
                    trade_id=trade.trade_id,
                    strategy_id=trade.strategy_id,
                    leg_label=f"L{index}",
                )
            await EventRepository(session).append(
                process="position",
                kind="POSITION_OPEN",
                detail={
                    "account_id": account.id,
                    "trade_id": trade.trade_id,
                    "strategy_id": trade.strategy_id,
                },
                signal_id=sig_row.id,
                idempotency_key=f"position_open:{account.id}:{trade.trade_id}",
            )
        logger.info(
            "POSITION_OPEN persisted: account_id=%s trade_id=%s legs=%s",
            resolved,
            trade.trade_id,
            [
                (leg.symbol, leg.side.value if hasattr(leg.side, "value") else leg.side, str(leg.quantity), str(leg.price))
                for leg in trade.legs
            ],
        )

    async def persist_close(
        self,
        signal: Signal,
        trade_id: str,
        orders: list[OMSOrder],
        *,
        account_id: int | None = None,
    ) -> OpenModelBlueTrade:
        resolved = self._account_id_from(orders, account_id)
        async with self._session_factory() as session, session.begin():
            row = await TradeRepository(session).get_row(
                trade_id, account_id=resolved
            )
            if row is None:
                raise KeyError(trade_id)
            assert_close_qty_matches_open(
                trade_id=trade_id,
                leg_a_symbol=row.leg_a_symbol,
                leg_a_signed_qty=row.leg_a_signed_qty,
                leg_b_symbol=row.leg_b_symbol,
                leg_b_signed_qty=row.leg_b_signed_qty,
                filled_by_symbol=_filled_qty_by_symbol(orders),
            )
            closed = await TradeRepository(session).close_trade(
                trade_id,
                account_id=resolved,
                exit_marks=_exit_marks_from_orders(orders),
                commission=_commission_from_orders(orders),
            )
            sig_row = await SignalRepository(session).record_processed(
                signal, persist_signal_id=f"{trade_id}:CLOSE"
            )
            order_repo = OrderRepository(session)
            for index, order in enumerate(orders):
                if getattr(order, "is_compensation", False):
                    continue
                await order_repo.record_oms_order(
                    order,
                    signal_pk=sig_row.id,
                    account_id=resolved,
                    trade_id=trade_id,
                    strategy_id=closed.strategy_id,
                    leg_label=f"L{index}",
                )
            await EventRepository(session).append(
                process="position",
                kind="POSITION_CLOSE",
                detail={
                    "account_id": resolved,
                    "trade_id": trade_id,
                },
                signal_id=sig_row.id,
                idempotency_key=f"position_close:{resolved}:{trade_id}",
            )
            logger.info(
                "POSITION_CLOSE persisted: account_id=%s trade_id=%s exit_marks=%s",
                resolved,
                trade_id,
                {k: str(v) for k, v in _exit_marks_from_orders(orders).items()},
            )
            return closed

    async def _require_account_allocation(
        self, session: AsyncSession, strategy_id: str, account_id: int
    ):
        alloc_repo = AllocationRepository(session)
        account = await alloc_repo.get_enabled_account(account_id)
        if account is None:
            raise ModelBlueValidationError(
                "MODEL_BLUE_ACCOUNT_MISSING: no enabled account row for persistence."
            )
        allocation = await alloc_repo.get_allocation(
            account_id=account.id, strategy_id=strategy_id
        )
        if allocation is None or not allocation.enabled:
            raise ModelBlueValidationError(
                "MODEL_BLUE_ALLOCATION_MISSING: no enabled allocations row for "
                f"account={account.id} strategy={strategy_id}."
            )
        return account, allocation
