"""Read-only snapshot of executed positions from PostgreSQL. Never writes."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.account import AccountModel
from app.db.models.basket import BasketModel
from app.db.models.order import OrderModel
from app.db.models.position import PositionModel

RISK_OPEN = "OPEN"
RISK_CLOSED = "CLOSED"


def _dec(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def _side(signed_qty: Decimal | None) -> str | None:
    if signed_qty is None:
        return None
    return "BUY" if signed_qty >= 0 else "SELL"


def _qty(signed_qty: Decimal | None) -> str | None:
    if signed_qty is None:
        return None
    return format(abs(signed_qty), "f")


def classify_event(
    *,
    previous_status: str | None,
    current_status: str,
    previous_fill: str | None,
    current_fill: str | None,
    close_in_progress: bool,
) -> str:
    """Map observed DB deltas to stream event names. Does not invent fills."""
    if previous_status is None and current_status == RISK_OPEN:
        return "POSITION_OPEN"
    if previous_status == RISK_OPEN and current_status == RISK_CLOSED:
        return "POSITION_CLOSED"
    if current_status == RISK_OPEN and close_in_progress:
        if previous_fill is not None and current_fill is not None and current_fill != previous_fill:
            return "POSITION_PARTIAL_CLOSE"
        return "POSITION_PARTIAL_CLOSE"
    return "POSITION_UPDATE"


def _order_for_symbol(orders: list[OrderModel], symbol: str) -> OrderModel | None:
    matches = [row for row in orders if row.symbol == symbol and not row.is_compensation]
    if not matches:
        return None
    working = [row for row in matches if row.status not in ("FILLED", "CANCELLED", "REJECTED", "ERROR")]
    pool = working or matches
    pool.sort(key=lambda row: row.id, reverse=True)
    return pool[0]


def _basket_state(baskets: list[BasketModel], position_status: str) -> str | None:
    if not baskets:
        return None
    by_action = {row.action.upper(): row.state for row in baskets}
    if position_status == RISK_CLOSED:
        return by_action.get("CLOSE") or by_action.get("OPEN")
    if "CLOSE" in by_action and by_action["CLOSE"] not in ("CLOSED",):
        return by_action["CLOSE"]
    return by_action.get("OPEN")


def _close_in_progress(baskets: list[BasketModel], orders: list[OrderModel]) -> bool:
    for row in baskets:
        if row.action.upper() == "CLOSE" and row.state in ("EXECUTING", "UNWINDING"):
            return True
    for row in orders:
        if row.is_compensation:
            continue
        if ":CLOSE" in (row.internal_order_id or "") and row.status not in ("CANCELLED", "REJECTED"):
            return True
    return False


def _leg_payload(
    *,
    position: PositionModel,
    account: AccountModel,
    symbol: str,
    signed_qty: Decimal | None,
    entry: Decimal | None,
    instrument_type: str | None,
    baskets: list[BasketModel],
    orders: list[OrderModel],
    timestamp: datetime,
) -> dict[str, Any]:
    order = _order_for_symbol(orders, symbol)
    close_in_progress = _close_in_progress(baskets, orders)
    filled = None
    if order is not None and order.fill_qty is not None:
        filled = _dec(order.fill_qty)
    elif signed_qty is not None:
        filled = _qty(signed_qty)
    live_pnl = _dec(position.live_pnl)
    market_status = "UNAVAILABLE"
    payload = {
        "timestamp": timestamp.isoformat(),
        "account_id": position.account_id,
        "ibkr_account": account.ibkr_account,
        "account_name": account.name,
        "strategy_id": position.strategy_id,
        "trade_id": position.trade_id,
        "symbol": symbol,
        "instrument_type": instrument_type,
        "side": _side(signed_qty),
        "quantity": _qty(signed_qty),
        "filled_quantity": filled,
        "entry_price": _dec(entry),
        "last_price": None,
        "mark_price": None,
        "unrealized_pnl": live_pnl,
        "realized_pnl": _dec(position.realised_pnl),
        "commission": _dec(position.commission),
        "status": position.risk_state,
        "basket_state": _basket_state(baskets, position.risk_state),
        "position_state": position.risk_state,
        "order_status": order.status if order is not None else None,
        "broker_order_id": order.broker_order_id if order is not None else None,
        "fill_status": order.status if order is not None else None,
        "fill_timestamp": order.filled_at.isoformat() if order is not None and order.filled_at else None,
        "market_data_status": market_status,
        "connection_status": "OBSERVING_DB",
        "close_in_progress": close_in_progress,
    }
    return payload


def position_leg_payloads(
    position: PositionModel,
    account: AccountModel,
    baskets: list[BasketModel],
    orders: list[OrderModel],
    *,
    timestamp: datetime,
) -> list[dict[str, Any]]:
    legs = [
        _leg_payload(
            position=position,
            account=account,
            symbol=position.leg_a_symbol,
            signed_qty=position.leg_a_signed_qty,
            entry=position.leg_a_entry_mark,
            instrument_type=position.leg_a_instrument_type,
            baskets=baskets,
            orders=orders,
            timestamp=timestamp,
        )
    ]
    if position.leg_b_symbol:
        legs.append(
            _leg_payload(
                position=position,
                account=account,
                symbol=position.leg_b_symbol,
                signed_qty=position.leg_b_signed_qty,
                entry=position.leg_b_entry_mark,
                instrument_type=position.leg_b_instrument_type,
                baskets=baskets,
                orders=orders,
                timestamp=timestamp,
            )
        )
    return legs


def fingerprint(payload: dict[str, Any]) -> tuple:
    return (
        payload.get("status"),
        payload.get("filled_quantity"),
        payload.get("unrealized_pnl"),
        payload.get("realized_pnl"),
        payload.get("commission"),
        payload.get("entry_price"),
        payload.get("basket_state"),
        payload.get("order_status"),
        payload.get("broker_order_id"),
        payload.get("close_in_progress"),
    )


async def load_position_rows(session: AsyncSession) -> list[tuple[PositionModel, AccountModel]]:
    result = await session.execute(
        select(PositionModel, AccountModel)
        .join(AccountModel, AccountModel.id == PositionModel.account_id)
        .where(PositionModel.risk_state == RISK_OPEN)
    )
    return list(result.all())


async def load_baskets(session: AsyncSession) -> dict[tuple[int, str], list[BasketModel]]:
    rows = (await session.execute(select(BasketModel))).scalars().all()
    grouped: dict[tuple[int, str], list[BasketModel]] = {}
    for row in rows:
        grouped.setdefault((row.account_id, row.trade_id), []).append(row)
    return grouped


async def load_orders(session: AsyncSession) -> dict[tuple[int, str], list[OrderModel]]:
    rows = (await session.execute(select(OrderModel))).scalars().all()
    grouped: dict[tuple[int, str], list[OrderModel]] = {}
    for row in rows:
        if not row.trade_id:
            continue
        grouped.setdefault((row.account_id, row.trade_id), []).append(row)
    return grouped
