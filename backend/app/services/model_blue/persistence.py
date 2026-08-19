"""Persist Model Blue execution state (signal + pair position + per-leg orders) atomically."""

import logging
from decimal import Decimal

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


def _exit_marks_from_orders(orders: list[OMSOrder]) -> dict[str, Decimal]:
    marks: dict[str, Decimal] = {}
    for order in orders:
        if getattr(order, "is_compensation", False):
            continue
        derived = executions_weighted_average(getattr(order, "executions", {}) or {})
        raw = derived or order.average_fill_price or order.last_fill_price
        if raw is None:
            continue
        marks[order.symbol] = raw if isinstance(raw, Decimal) else Decimal(str(raw))
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


def _open_trade_from_fills(
    trade: OpenModelBlueTrade, orders: list[OMSOrder]
) -> OpenModelBlueTrade:
    """Build the pair row from actual FILLED broker quantities and avg prices."""
    fill_orders = [o for o in orders if not getattr(o, "is_compensation", False)]
    fill_orders.sort(key=lambda o: (o.leg_index is None, o.leg_index or 0))
    if len(fill_orders) != 2:
        raise ModelBlueValidationError(
            "POSITION_REQUIRES_FILLS: Model Blue OPEN persists only after both "
            f"legs are filled (got {len(fill_orders)} child orders)."
        )
    legs: list[OpenModelBlueTradeLeg] = []
    for order in fill_orders:
        if order.status != OMSOrderStatus.FILLED:
            raise ModelBlueValidationError(
                f"POSITION_REQUIRES_FILLS: {order.symbol} status={order.status.value}."
            )
        qty = Decimal(str(order.filled_quantity))
        if qty <= 0:
            raise ModelBlueValidationError(
                f"POSITION_REQUIRES_FILLS: {order.symbol} filled_quantity={qty}."
            )
        derived = executions_weighted_average(getattr(order, "executions", {}) or {})
        raw = derived or order.average_fill_price or order.last_fill_price
        if raw is None:
            raise ModelBlueValidationError(
                f"POSITION_REQUIRES_FILLS: {order.symbol} has no fill price."
            )
        price = raw if isinstance(raw, Decimal) else Decimal(str(raw))
        resolved = getattr(order, "resolved", None)
        itype = None
        if resolved is not None:
            # Persist the executed IBKR product, not the TradingView requested type.
            itype = resolved.sec_type
        elif order.leg_index is not None and order.intent.legs:
            if 0 <= order.leg_index < len(order.intent.legs):
                itype = order.intent.legs[order.leg_index].instrument_type
        if not itype:
            raise ModelBlueValidationError(
                f"POSITION_REQUIRES_INSTRUMENT_TYPE: {order.symbol} fill has no instrument_type."
            )
        legs.append(
            OpenModelBlueTradeLeg(
                symbol=order.symbol,
                instrument_type=itype,
                side=order.side,
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
            await TradeRepository(session).open_trade(
                trade,
                account_id=account.id,
                target=allocation.target,
                stop=allocation.stop,
                time_limit=allocation.time_limit,
            )
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
