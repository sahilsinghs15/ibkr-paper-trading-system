"""Read-only snapshot of executed positions from PostgreSQL. Never writes."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.account import AccountModel
from app.db.models.basket import BasketModel
from app.db.models.event import EventLogModel
from app.db.models.execution import ExecutionModel
from app.db.models.order import OrderModel
from app.db.models.position import PositionModel
from app.db.models.signal import SignalModel

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
    live_pnl = _dec(position.live_pnl) if position.risk_state == RISK_OPEN else None
    realized_pnl = (
        _dec(position.realised_pnl)
        if (position.risk_state == RISK_CLOSED or position.realised_pnl != Decimal(0))
        else None
    )
    opened_ts = position.opened_at if getattr(position, "opened_at", None) is not None else timestamp
    closed_ts = getattr(position, "closed_at", None)
    if closed_ts is None and order is not None and order.filled_at is not None and position.risk_state == RISK_CLOSED:
        closed_ts = order.filled_at

    market_status = "UNAVAILABLE"
    payload = {
        "timestamp": opened_ts.isoformat(),
        "opened_at": opened_ts.isoformat() if opened_ts else None,
        "closed_at": closed_ts.isoformat() if closed_ts else None,
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
        "realized_pnl": realized_pnl,
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
        payload.get("opened_at"),
        payload.get("closed_at"),
    )


async def load_position_rows(session: AsyncSession) -> list[tuple[PositionModel, AccountModel]]:
    result = await session.execute(
        select(PositionModel, AccountModel)
        .join(AccountModel, AccountModel.id == PositionModel.account_id)
        .where(PositionModel.risk_state == RISK_OPEN)
    )
    return list(result.all())


async def load_closed_position_rows(
    session: AsyncSession, account_id: int | None = None
) -> list[tuple[PositionModel, AccountModel]]:
    """Load historical CLOSED positions from PostgreSQL."""
    stmt = (
        select(PositionModel, AccountModel)
        .join(AccountModel, AccountModel.id == PositionModel.account_id)
        .where(PositionModel.risk_state == RISK_CLOSED)
        .order_by(PositionModel.closed_at.desc())
        .limit(100)
    )
    if account_id is not None:
        stmt = stmt.where(PositionModel.account_id == account_id)
    result = await session.execute(stmt)
    return list(result.all())


async def load_position_with_account(
    session: AsyncSession, account_id: int, trade_id: str
) -> tuple[PositionModel, AccountModel] | None:
    """Load a position in any risk_state. Used so CLOSE events carry realised P&L."""
    result = await session.execute(
        select(PositionModel, AccountModel)
        .join(AccountModel, AccountModel.id == PositionModel.account_id)
        .where(
            PositionModel.account_id == account_id,
            PositionModel.trade_id == trade_id,
        )
    )
    return result.first()


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


async def load_signals(session: AsyncSession, limit: int = 50) -> list[dict[str, Any]]:
    if session is None or not hasattr(session, "execute"):
        return []
    rows = (
        await session.execute(
            select(SignalModel)
            .order_by(SignalModel.received_at.desc())
            .limit(limit)
        )
    ).scalars().all()

    if not rows:
        return []

    sig_ids = [s.id for s in rows]
    trade_ids = [s.trade_id for s in rows if s.trade_id]

    # 1. Batched query for child orders
    order_stmt = select(OrderModel).where(
        (OrderModel.signal_id.in_(sig_ids)) | (OrderModel.trade_id.in_(trade_ids))
    )
    all_orders = (await session.execute(order_stmt)).scalars().all()

    orders_by_sig_id: dict[int, list[OrderModel]] = {}
    orders_by_trade_id: dict[str, list[OrderModel]] = {}
    order_ids: list[int] = []
    internal_order_ids: list[str] = []
    for o in all_orders:
        if o.id:
            order_ids.append(o.id)
        if o.internal_order_id:
            internal_order_ids.append(o.internal_order_id)
        if o.signal_id:
            orders_by_sig_id.setdefault(o.signal_id, []).append(o)
        if o.trade_id:
            orders_by_trade_id.setdefault(o.trade_id, []).append(o)

    # 2. Batched query for execution fill reports
    all_executions: list[ExecutionModel] = []
    if order_ids or internal_order_ids:
        exec_stmt = select(ExecutionModel).where(
            (ExecutionModel.order_id.in_(order_ids))
            | (ExecutionModel.internal_order_id.in_(internal_order_ids))
        )
        all_executions = (await session.execute(exec_stmt)).scalars().all()

    execs_by_order_id: dict[int, list[ExecutionModel]] = {}
    execs_by_internal_id: dict[str, list[ExecutionModel]] = {}
    for ex in all_executions:
        if ex.order_id:
            execs_by_order_id.setdefault(ex.order_id, []).append(ex)
        if ex.internal_order_id:
            execs_by_internal_id.setdefault(ex.internal_order_id, []).append(ex)

    # 3. Batched query for audit event log records
    all_events: list[EventLogModel] = []
    event_stmt = select(EventLogModel).where(
        (EventLogModel.signal_id.in_(sig_ids))
        | (EventLogModel.order_id.in_(order_ids))
    )
    all_events = (await session.execute(event_stmt)).scalars().all()

    events_by_sig_id: dict[int, list[EventLogModel]] = {}
    events_by_trade_id: dict[str, list[EventLogModel]] = {}
    for ev in all_events:
        if ev.signal_id:
            events_by_sig_id.setdefault(ev.signal_id, []).append(ev)
        tid = str((ev.detail or {}).get("trade_id") or "").strip()
        if tid:
            events_by_trade_id.setdefault(tid, []).append(ev)

    signals: list[dict[str, Any]] = []
    for sig in rows:
        pair = sig.pair
        if (not pair or pair == "N/A") and sig.trade_id:
            trade_parts = [p.split(":")[-1] for p in sig.trade_id.split("-") if ":" in p]
            if len(trade_parts) >= 2:
                pair = f"{trade_parts[0]} / {trade_parts[1]}"
            elif len(trade_parts) == 1:
                pair = trade_parts[0]
        elif pair and ":" in pair and " / " not in pair:
            pair = pair.replace(":", " / ")

        matched_orders = orders_by_sig_id.get(sig.id) or (orders_by_trade_id.get(sig.trade_id) if sig.trade_id else []) or []
        matched_orders = sorted(matched_orders, key=lambda x: x.id)

        orders_payload = []
        for o in matched_orders:
            m_execs = execs_by_order_id.get(o.id) or (execs_by_internal_id.get(o.internal_order_id) if o.internal_order_id else []) or []
            m_execs = sorted(m_execs, key=lambda x: x.id)

            execs_payload = [
                {
                    "id": ex.id,
                    "exec_id": ex.exec_id,
                    "symbol": ex.symbol,
                    "side": ex.side,
                    "quantity": float(ex.quantity) if ex.quantity is not None else 0.0,
                    "price": float(ex.price) if ex.price is not None else 0.0,
                    "executed_at": ex.executed_at.isoformat() if ex.executed_at else ex.created_at.isoformat() if ex.created_at else None,
                }
                for ex in m_execs
            ]

            orders_payload.append(
                {
                    "id": o.id,
                    "internal_order_id": o.internal_order_id,
                    "leg": o.leg,
                    "symbol": o.symbol,
                    "buy_sell": o.buy_sell,
                    "quantity": float(o.quantity) if o.quantity is not None else 0.0,
                    "fill_qty": float(o.fill_qty) if o.fill_qty is not None else 0.0,
                    "fill_price": float(o.fill_price) if o.fill_price is not None else None,
                    "status": o.status,
                    "is_compensation": o.is_compensation,
                    "compensation_of_internal_order_id": o.compensation_of_internal_order_id,
                    "filled_at": o.filled_at.isoformat() if o.filled_at else None,
                    "executions": execs_payload,
                }
            )

        matched_events = events_by_sig_id.get(sig.id) or (events_by_trade_id.get(sig.trade_id) if sig.trade_id else []) or []
        matched_events = sorted(matched_events, key=lambda x: x.id)

        events_payload = [
            {
                "id": ev.id,
                "kind": ev.kind,
                "ts": ev.ts.isoformat() if ev.ts else None,
                "detail": ev.detail or {},
            }
            for ev in matched_events
        ]

        # Reconcile canonical lifecycle status & processing duration
        c_status, is_active, rec_reason, calc_proc_at, duration_sec = reconcile_signal_status(
            sig, orders_payload, events_payload
        )

        signals.append(
            {
                "id": sig.id,
                "signal_id": sig.signal_id,
                "trade_id": sig.trade_id,
                "strategy_id": sig.strategy_id,
                "action": sig.action,
                "pair": pair or "N/A",
                "side": sig.side,
                "status": sig.status,
                "canonical_status": c_status,
                "is_active_processing": is_active,
                "reconciled_reason": rec_reason,
                "reject_reason": sig.reject_reason or rec_reason,
                "received_at": sig.received_at.isoformat() if sig.received_at else None,
                "processed_at": calc_proc_at,
                "processing_duration_sec": duration_sec,
                "raw_payload": sig.raw_payload,
                "orders": orders_payload,
                "events": events_payload,
            }
        )
    return signals


def reconcile_signal_status(
    sig: SignalModel,
    orders: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> tuple[str, bool, str | None, str | None, float | None]:
    """Reconcile raw database status to canonical_status, is_active_processing, reason, processed_at, duration_sec."""
    raw_status = str(sig.status or "").upper()
    reject_reason = sig.reject_reason

    # 1. RMS / OMS Rejections
    has_reject_event = any(
        e.get("kind") in ("RMS_REJECTED", "OMS_REJECTED", "SIGNAL_REJECTED", "EXECUTION_ERROR")
        for e in events
    )
    if reject_reason or has_reject_event or raw_status in ("REJECTED", "ERROR", "FAILED"):
        proc_at = sig.processed_at.isoformat() if sig.processed_at else (events[-1]["ts"] if events and events[-1].get("ts") else (sig.received_at.isoformat() if sig.received_at else None))
        rec_reason = reject_reason or "Declined by RMS/OMS execution pipeline"
        duration = None
        if sig.received_at and proc_at:
            try:
                dt_rec = sig.received_at
                dt_proc = datetime.fromisoformat(proc_at)
                duration = max(0.0, (dt_proc - dt_rec).total_seconds())
            except Exception:
                pass
        return ("REJECTED", False, rec_reason, proc_at, duration)

    # 2. Compensation / Unwinding Orders
    primary_orders = [o for o in orders if not o.get("is_compensation")]
    comp_orders = [o for o in orders if o.get("is_compensation")]

    has_comp_unwound = any(
        o.get("status") in ("FILLED", "CANCELLED") for o in comp_orders
    ) or any(e.get("kind") in ("BASKET_UNWINDING", "SQUARE_OFF") for e in events)

    if has_comp_unwound or (comp_orders and all(o.get("status") in ("FILLED", "CANCELLED") for o in comp_orders)):
        proc_at = sig.processed_at.isoformat() if sig.processed_at else (events[-1]["ts"] if events and events[-1].get("ts") else (sig.received_at.isoformat() if sig.received_at else None))
        duration = None
        if sig.received_at and proc_at:
            try:
                dt_rec = sig.received_at
                dt_proc = datetime.fromisoformat(proc_at)
                duration = max(0.0, (dt_proc - dt_rec).total_seconds())
            except Exception:
                pass
        return ("SQUARE-OFF", False, "Incomplete leg timeout reached — Exposure automatically squared off", proc_at, duration)

    # 3. Primary Orders Full Fill
    if primary_orders:
        all_filled = all(
            (o.get("status") in ("FILLED", "PROCESSED", "SUCCESS") or (o.get("fill_qty", 0.0) >= o.get("quantity", 1.0) > 0))
            for o in primary_orders
        )
        if all_filled:
            last_fill_ts = None
            for o in primary_orders:
                if o.get("filled_at"):
                    if not last_fill_ts or o["filled_at"] > last_fill_ts:
                        last_fill_ts = o["filled_at"]
            proc_at = sig.processed_at.isoformat() if sig.processed_at else (last_fill_ts or (sig.received_at.isoformat() if sig.received_at else None))
            duration = None
            if sig.received_at and proc_at:
                try:
                    dt_rec = sig.received_at
                    dt_proc = datetime.fromisoformat(proc_at)
                    duration = max(0.0, (dt_proc - dt_rec).total_seconds())
                except Exception:
                    pass
            return ("ACCEPTED", False, None, proc_at, duration)

        # Active Working Orders
        has_working = any(
            o.get("status") in ("SUBMITTED", "PRESUBMITTED", "PENDING", "PARTIALLY_FILLED", "RETRYING")
            for o in primary_orders
        )
        if has_working:
            return ("PROCESSING", True, "Working orders executing in IBKR", None, None)

    # 4. Explicit PROCESSED status in DB
    if raw_status in ("PROCESSED", "FILLED", "SUCCESS"):
        proc_at = sig.processed_at.isoformat() if sig.processed_at else (sig.received_at.isoformat() if sig.received_at else None)
        duration = None
        if sig.received_at and proc_at:
            try:
                dt_rec = sig.received_at
                dt_proc = datetime.fromisoformat(proc_at)
                duration = max(0.0, (dt_proc - dt_rec).total_seconds())
            except Exception:
                pass
        return ("ACCEPTED", False, None, proc_at, duration)

    # 5. Stale / Orphaned Signals (e.g. status == NEW but no active working orders)
    proc_at = sig.processed_at.isoformat() if sig.processed_at else (sig.received_at.isoformat() if sig.received_at else None)
    duration = None
    if sig.received_at and proc_at:
        try:
            dt_rec = sig.received_at
            dt_proc = datetime.fromisoformat(proc_at)
            duration = max(0.0, (dt_proc - dt_rec).total_seconds())
        except Exception:
            pass

    if primary_orders and any(o.get("status") in ("CANCELLED", "REJECTED", "ERROR") for o in primary_orders):
        return ("REJECTED", False, "Broker leg order rejected or cancelled", proc_at, duration)

    # Truthful handling when no orders or execution evidence exists:
    return ("EXPIRED", False, "Signal inactive — No broker orders or execution reports recorded", proc_at, duration)
