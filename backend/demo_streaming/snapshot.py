"""Read-only snapshot of executed positions from PostgreSQL. Never writes."""

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.account import AccountModel
from app.db.models.basket import BasketModel
from app.db.models.event import EventLogModel
from app.db.models.execution import ExecutionModel
from app.db.models.order import OrderModel
from app.db.models.position import PositionModel
from app.db.models.signal import SignalJobModel, SignalModel

RISK_OPEN = "OPEN"
RISK_CLOSED = "CLOSED"


def _norm_ibkr(value: str | None) -> str | None:
    if value is None:
        return None
    s = str(value).strip().upper()
    return s or None


def _payload_matches_account(
    raw_payload: Any,
    target_acc_id: int | None,
    target_ibkr_acc: str | None,
) -> bool:
    if not raw_payload or not isinstance(raw_payload, dict):
        return False
    p_acc = raw_payload.get("account") or raw_payload.get("ibkr_account") or raw_payload.get("account_id")
    if p_acc is None or str(p_acc).strip() == "":
        return False
    p_str = str(p_acc).strip()
    if target_ibkr_acc and p_str.upper() == target_ibkr_acc.upper():
        return True
    if target_acc_id is not None and p_str.isdigit() and int(p_str) == target_acc_id:
        return True
    return False


def _dec(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def _quantize_pnl(value: Decimal | None) -> Decimal | None:
    """Round OPEN live_pnl to cents so sub-cent IBKR ticks do not churn fingerprints."""
    if value is None:
        return None
    return value.quantize(Decimal("0.01"))


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


def _basket_id_for_action(baskets: list[BasketModel], action: str) -> int | None:
    for row in baskets:
        if row.action.upper() == action:
            return row.id
    return None


def _order_for_symbol(
    orders: list[OrderModel], symbol: str, *, basket_id: int | None = None
) -> OrderModel | None:
    matches = [row for row in orders if row.symbol == symbol and not row.is_compensation]
    if basket_id is not None:
        scoped = [row for row in matches if row.basket_id == basket_id]
        if scoped:
            matches = scoped
    if not matches:
        return None
    return max(matches, key=lambda row: row.id)


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
    entry_order = _order_for_symbol(
        orders, symbol, basket_id=_basket_id_for_action(baskets, "OPEN")
    )
    closing_order = _order_for_symbol(
        orders, symbol, basket_id=_basket_id_for_action(baskets, "CLOSE")
    )
    close_in_progress = _close_in_progress(baskets, orders)
    if position.risk_state == RISK_OPEN:
        filled = _qty(signed_qty)
    elif entry_order is not None and entry_order.fill_qty is not None:
        filled = _dec(entry_order.fill_qty)
    else:
        filled = _qty(signed_qty)
    live_pnl = (
        _dec(_quantize_pnl(position.live_pnl))
        if position.risk_state == RISK_OPEN
        else None
    )
    realized_pnl = (
        _dec(position.realised_pnl)
        if (position.risk_state == RISK_CLOSED or position.realised_pnl != Decimal(0))
        else None
    )
    opened_ts = position.opened_at if getattr(position, "opened_at", None) is not None else timestamp
    closed_ts = getattr(position, "closed_at", None)
    close_fill_order = closing_order or entry_order
    if (
        closed_ts is None
        and close_fill_order is not None
        and close_fill_order.filled_at is not None
        and position.risk_state == RISK_CLOSED
    ):
        closed_ts = close_fill_order.filled_at

    market_status = "LIVE" if position.live_pnl is not None and position.live_pnl != Decimal(0) else "UNAVAILABLE"
    payload = {
        "timestamp": timestamp.isoformat(),
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
        "order_status": entry_order.status if entry_order is not None else None,
        "broker_order_id": entry_order.broker_order_id if entry_order is not None else None,
        "fill_status": entry_order.status if entry_order is not None else None,
        "fill_timestamp": (
            entry_order.filled_at.isoformat()
            if entry_order is not None and entry_order.filled_at
            else None
        ),
        "closing_order_status": closing_order.status if closing_order is not None else None,
        "closing_broker_order_id": (
            closing_order.broker_order_id if closing_order is not None else None
        ),
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


_VOLATILE_PAYLOAD_KEYS = frozenset({"timestamp"})
_PNL_ONLY_PAYLOAD_KEYS = frozenset({"timestamp", "unrealized_pnl", "market_data_status"})


def fingerprint(payload: dict[str, Any]) -> tuple:
    """Full payload fingerprint (legacy). Prefer structural_fingerprint + pnl_fingerprint."""
    stable = {k: v for k, v in payload.items() if k not in _VOLATILE_PAYLOAD_KEYS}
    return (json.dumps(stable, sort_keys=True, default=str),)


def structural_fingerprint(payload: dict[str, Any]) -> tuple:
    """Fingerprint excluding timestamp and PnL fields (fills, status, etc.)."""
    stable = {k: v for k, v in payload.items() if k not in _PNL_ONLY_PAYLOAD_KEYS}
    return (json.dumps(stable, sort_keys=True, default=str),)


def pnl_fingerprint(payload: dict[str, Any]) -> tuple:
    """Fingerprint for quantized unrealized_pnl only."""
    return (payload.get("unrealized_pnl"),)


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


async def load_baskets(
    session: AsyncSession,
    keys: set[tuple[int, str]] | None = None,
) -> dict[tuple[int, str], list[BasketModel]]:
    if keys is not None and not keys:
        return {}
    stmt = select(BasketModel)
    if keys is not None:
        stmt = stmt.where(tuple_(BasketModel.account_id, BasketModel.trade_id).in_(list(keys)))
    rows = (await session.execute(stmt)).scalars().all()
    grouped: dict[tuple[int, str], list[BasketModel]] = {}
    for row in rows:
        grouped.setdefault((row.account_id, row.trade_id), []).append(row)
    return grouped


async def load_orders(
    session: AsyncSession,
    keys: set[tuple[int, str]] | None = None,
) -> dict[tuple[int, str], list[OrderModel]]:
    if keys is not None and not keys:
        return {}
    stmt = select(OrderModel).where(OrderModel.trade_id.is_not(None))
    if keys is not None:
        stmt = stmt.where(tuple_(OrderModel.account_id, OrderModel.trade_id).in_(list(keys)))
    rows = (await session.execute(stmt)).scalars().all()
    grouped: dict[tuple[int, str], list[OrderModel]] = {}
    for row in rows:
        if not row.trade_id:
            continue
        grouped.setdefault((row.account_id, row.trade_id), []).append(row)
    return grouped


async def load_signals(
    session: AsyncSession,
    limit: int | None = None,
    *,
    page: int = 1,
    page_size: int = 100,
    status_filter: str | None = None,
    account_id: int | None = None,
    ibkr_account: str | None = None,
    return_dict: bool = False,
    for_watch: bool = False,
) -> dict[str, Any] | list[dict[str, Any]]:
    if session is None or not hasattr(session, "execute"):
        return {"signals": [], "page": 1, "page_size": page_size, "total": 0, "total_pages": 1, "counts": {"total": 0, "processing": 0, "accepted": 0, "rejected": 0, "square_off": 0}} if return_dict else []

    eff_page_size = page_size if limit is None else limit
    eff_page_size = max(1, min(eff_page_size, 500))
    eff_page = max(1, page)

    # Load Account map for account_id <-> ibkr_account resolution
    acc_rows = (await session.execute(select(AccountModel))).scalars().all()
    acc_by_id: dict[int, AccountModel] = {a.id: a for a in acc_rows}
    acc_by_ibkr: dict[str, AccountModel] = {
        a.ibkr_account.strip().upper(): a for a in acc_rows if a.ibkr_account
    }

    target_acc_id: int | None = account_id
    target_ibkr_acc: str | None = _norm_ibkr(ibkr_account)
    if target_ibkr_acc and not target_acc_id:
        acc_obj = acc_by_ibkr.get(target_ibkr_acc)
        if acc_obj:
            target_acc_id = acc_obj.id
    elif target_acc_id and not target_ibkr_acc:
        acc_obj = acc_by_id.get(target_acc_id)
        if acc_obj:
            target_ibkr_acc = _norm_ibkr(acc_obj.ibkr_account)
    target_ibkr_canonical = (
        acc_by_id[target_acc_id].ibkr_account
        if target_acc_id is not None and target_acc_id in acc_by_id
        else (ibkr_account.strip() if ibkr_account else None)
    )

    stmt = select(SignalModel)
    job_scopes_by_sig: dict[str, list[str]] = {}
    if target_acc_id is not None or target_ibkr_acc:
        matched_sig_ids: set[int] = set()

        # a) OrderModel matching
        order_subq_stmt = select(OrderModel.signal_id, OrderModel.account_id).where(OrderModel.signal_id.is_not(None))
        if target_acc_id is not None:
            order_subq_stmt = order_subq_stmt.where(OrderModel.account_id == target_acc_id)
        order_res = (await session.execute(order_subq_stmt)).all()
        for s_id, _a_id in order_res:
            if s_id:
                matched_sig_ids.add(s_id)

        # b) SignalJobModel matching
        job_stmt = select(SignalJobModel.signal_id, SignalJobModel.account_scope)
        job_res = (await session.execute(job_stmt)).all()
        for sig_str_id, acc_scope in job_res:
            if not acc_scope:
                continue
            scope_str = str(acc_scope).strip()
            job_scopes_by_sig.setdefault(str(sig_str_id), []).append(scope_str)
            if (target_acc_id and scope_str == str(target_acc_id)) or (
                target_ibkr_acc and scope_str.upper() == target_ibkr_acc.upper()
            ):
                s_stmt = select(SignalModel.id).where(SignalModel.signal_id == sig_str_id)
                s_res = (await session.execute(s_stmt)).scalars().all()
                matched_sig_ids.update(s_res)

        # c) PositionModel matching by trade_id
        if target_acc_id is not None:
            pos_stmt = select(PositionModel.trade_id).where(PositionModel.account_id == target_acc_id)
            pos_trades = set((await session.execute(pos_stmt)).scalars().all())
            if pos_trades:
                s_pos_stmt = select(SignalModel.id).where(SignalModel.trade_id.in_(pos_trades))
                s_pos_res = (await session.execute(s_pos_stmt)).scalars().all()
                matched_sig_ids.update(s_pos_res)

        # d) Raw payload matching
        payload_stmt = select(SignalModel.id, SignalModel.raw_payload)
        p_res = (await session.execute(payload_stmt)).all()
        for s_id, r_payload in p_res:
            if not r_payload or not isinstance(r_payload, dict):
                continue
            p_acc = r_payload.get("account") or r_payload.get("ibkr_account") or r_payload.get("account_id")
            if p_acc:
                p_str = str(p_acc).strip().upper()
                if (target_ibkr_acc and p_str == target_ibkr_acc.upper()) or (target_acc_id and p_str == str(target_acc_id)):
                    matched_sig_ids.add(s_id)

        stmt = stmt.where(SignalModel.id.in_(matched_sig_ids))

    stmt = stmt.order_by(SignalModel.received_at.desc(), SignalModel.id.desc())

    if for_watch:
        stmt = stmt.limit(eff_page_size)

    all_rows = (await session.execute(stmt)).scalars().all()
    if not all_rows:
        empty_res = {"signals": [], "page": eff_page, "page_size": eff_page_size, "total": 0, "total_pages": 1, "counts": {"total": 0, "processing": 0, "accepted": 0, "rejected": 0, "square_off": 0}}
        return empty_res if return_dict else []

    # To calculate canonical counts & filter accurately, we batch-load order & event info for all_rows
    all_sig_ids = [s.id for s in all_rows]

    order_stmt = select(OrderModel).where(OrderModel.signal_id.in_(all_sig_ids))
    all_orders = (await session.execute(order_stmt)).scalars().all()

    orders_by_sig_id: dict[int, list[OrderModel]] = {}
    signal_id_by_order_id: dict[int, int] = {}
    order_ids: list[int] = []
    internal_order_ids: list[str] = []
    for o in all_orders:
        if o.id:
            order_ids.append(o.id)
            signal_id_by_order_id[o.id] = o.signal_id
        if o.internal_order_id:
            internal_order_ids.append(o.internal_order_id)
        orders_by_sig_id.setdefault(o.signal_id, []).append(o)

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

    event_stmt = select(EventLogModel).where(
        (EventLogModel.signal_id.in_(all_sig_ids))
        | (EventLogModel.order_id.in_(order_ids))
    )
    all_events = (await session.execute(event_stmt)).scalars().all()

    events_by_sig_id: dict[int, list[EventLogModel]] = {}
    for ev in all_events:
        owner = ev.signal_id or signal_id_by_order_id.get(ev.order_id or 0)
        if owner:
            events_by_sig_id.setdefault(owner, []).append(ev)

    # Build full reconciled signals list
    all_reconciled: list[dict[str, Any]] = []
    count_processing = 0
    count_accepted = 0
    count_rejected = 0
    count_square_off = 0

    for sig in all_rows:
        pair = sig.pair
        if (not pair or pair == "N/A") and sig.trade_id:
            trade_parts = [p.split(":")[-1] for p in sig.trade_id.split("-") if ":" in p]
            if len(trade_parts) >= 2:
                pair = f"{trade_parts[0]} / {trade_parts[1]}"
            elif len(trade_parts) == 1:
                pair = trade_parts[0]
        elif pair and ":" in pair and " / " not in pair:
            pair = pair.replace(":", " / ")

        matched_orders = orders_by_sig_id.get(sig.id, [])
        matched_orders = sorted(matched_orders, key=lambda x: x.id)

        sig_acc_id: int | None = None
        sig_ibkr_acc: str | None = None
        if target_acc_id is not None or target_ibkr_acc:
            account_orders = [
                o for o in matched_orders
                if target_acc_id is None or o.account_id == target_acc_id
            ]
            payload_match = _payload_matches_account(sig.raw_payload, target_acc_id, target_ibkr_acc)
            job_match = False
            for scope_str in job_scopes_by_sig.get(str(sig.signal_id), []):
                if target_acc_id is not None and scope_str == str(target_acc_id):
                    job_match = True
                    break
                if target_ibkr_acc and scope_str.upper() == target_ibkr_acc.upper():
                    job_match = True
                    break
            if account_orders:
                matched_orders = account_orders
            elif job_match or payload_match:
                matched_orders = []
            else:
                continue
            sig_acc_id = target_acc_id
            sig_ibkr_acc = target_ibkr_canonical

        scoped_order_ids = {o.id for o in matched_orders if o.id}

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
                    "basket_id": o.basket_id,
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

        matched_events = events_by_sig_id.get(sig.id, [])
        if target_acc_id is not None or target_ibkr_acc:
            matched_events = [
                ev for ev in matched_events
                if ev.order_id is None or ev.order_id in scoped_order_ids
            ]
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

        c_status, is_active, rec_reason, calc_proc_at, duration_sec = reconcile_signal_status(
            sig, orders_payload, events_payload
        )

        if not sig_ibkr_acc and matched_orders:
            for mo in matched_orders:
                if mo.account_id:
                    sig_acc_id = mo.account_id
                    acc_obj = acc_by_id.get(mo.account_id)
                    if acc_obj:
                        sig_ibkr_acc = acc_obj.ibkr_account
                    break

        if not sig_ibkr_acc and matched_events:
            for ev in matched_events:
                detail = ev.detail or {}
                if isinstance(detail, dict) and detail.get("ibkr_account"):
                    sig_ibkr_acc = str(detail["ibkr_account"])
                    if detail.get("account_id"):
                        try:
                            sig_acc_id = int(detail["account_id"])
                        except (ValueError, TypeError):
                            pass
                    break

        if not sig_ibkr_acc and sig.raw_payload and isinstance(sig.raw_payload, dict):
            p_acc = sig.raw_payload.get("account") or sig.raw_payload.get("ibkr_account") or sig.raw_payload.get("account_id")
            if p_acc:
                p_str = str(p_acc).strip()
                if p_str.isdigit():
                    sig_acc_id = int(p_str)
                    acc_obj = acc_by_id.get(sig_acc_id)
                    if acc_obj:
                        sig_ibkr_acc = acc_obj.ibkr_account
                else:
                    sig_ibkr_acc = p_str
                    acc_obj = acc_by_ibkr.get(p_str.upper())
                    if acc_obj:
                        sig_acc_id = acc_obj.id

        if target_ibkr_acc and sig_ibkr_acc and _norm_ibkr(sig_ibkr_acc) != target_ibkr_acc:
            continue
        if target_acc_id is not None and sig_acc_id is not None and sig_acc_id != target_acc_id:
            continue

        if not for_watch:
            if c_status == "PROCESSING":
                count_processing += 1
            elif c_status == "ACCEPTED":
                count_accepted += 1
            elif c_status == "SQUARE-OFF":
                count_square_off += 1
                count_rejected += 1
            else:
                count_rejected += 1

        all_reconciled.append(
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
                "account_id": sig_acc_id,
                "ibkr_account": sig_ibkr_acc,
                "raw_payload": sig.raw_payload,
                "orders": orders_payload,
                "events": events_payload,
            }
        )

    if for_watch:
        if not return_dict:
            return all_reconciled
        return {
            "signals": all_reconciled,
            "page": 1,
            "page_size": eff_page_size,
            "total": len(all_reconciled),
            "filtered_total": len(all_reconciled),
            "total_pages": 1,
            "counts": {
                "total": 0,
                "processing": 0,
                "accepted": 0,
                "rejected": 0,
                "square_off": 0,
            },
        }

    total_count = len(all_reconciled)

    # Filter by canonical status if requested
    filtered = all_reconciled
    if status_filter and status_filter.upper() != "ALL":
        sf = status_filter.upper()
        if sf == "REJECTED":
            filtered = [s for s in all_reconciled if s["canonical_status"] in ("REJECTED", "SQUARE-OFF", "EXPIRED")]
        else:
            filtered = [s for s in all_reconciled if s["canonical_status"] == sf]

    effective_total = len(filtered)
    import math
    total_pages = max(1, math.ceil(effective_total / eff_page_size))
    start_idx = (eff_page - 1) * eff_page_size
    page_signals = filtered[start_idx : start_idx + eff_page_size]

    if not return_dict:
        return page_signals

    return {
        "signals": page_signals,
        "page": eff_page,
        "page_size": eff_page_size,
        "total": total_count,
        "filtered_total": effective_total,
        "total_pages": total_pages,
        "counts": {
            "total": total_count,
            "processing": count_processing,
            "accepted": count_accepted,
            "rejected": count_rejected,
            "square_off": count_square_off,
        },
    }


def _last_fill_ts(orders: list[dict[str, Any]]) -> str | None:
    """Latest filled_at across the given orders. ISO-8601 strings compare lexicographically."""
    stamps = [o["filled_at"] for o in orders if o.get("filled_at")]
    return max(stamps) if stamps else None


def _duration_sec(received_at: datetime | None, processed_at: str | None) -> float | None:
    if not received_at or not processed_at:
        return None
    try:
        return max(0.0, (datetime.fromisoformat(processed_at) - received_at).total_seconds())
    except (TypeError, ValueError):
        return None


def reconcile_signal_status(
    sig: SignalModel,
    orders: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> tuple[str, bool, str | None, str | None, float | None]:
    """Reconcile raw database status to canonical_status, is_active_processing, reason, processed_at, duration_sec."""
    raw_status = str(sig.status or "").upper()
    reject_reason = sig.reject_reason
    db_processed = sig.processed_at.isoformat() if sig.processed_at else None

    # 1. RMS / OMS Rejections
    reject_event = next(
        (
            e
            for e in events
            if e.get("kind") in ("RMS_REJECTED", "OMS_REJECTED", "SIGNAL_REJECTED", "EXECUTION_ERROR")
        ),
        None,
    )
    has_reject_event = reject_event is not None
    if reject_reason or has_reject_event or raw_status in ("REJECTED", "ERROR", "FAILED"):
        reject_event_ts = reject_event.get("ts") if reject_event else None
        proc_at = db_processed or reject_event_ts
        rec_reason = reject_reason or "Declined by RMS/OMS execution pipeline"
        duration = _duration_sec(sig.received_at, proc_at)
        return ("REJECTED", False, rec_reason, proc_at, duration)

    # 2. Compensation / Unwinding Orders
    primary_orders = [o for o in orders if not o.get("is_compensation")]
    comp_orders = [o for o in orders if o.get("is_compensation")]

    unwind_event = next(
        (e for e in events if e.get("kind") in ("BASKET_UNWINDING", "SQUARE_OFF")),
        None,
    )
    has_comp_unwound = any(
        o.get("status") in ("FILLED", "CANCELLED") for o in comp_orders
    ) or unwind_event is not None

    if has_comp_unwound or (comp_orders and all(o.get("status") in ("FILLED", "CANCELLED") for o in comp_orders)):
        unwind_event_ts = unwind_event.get("ts") if unwind_event else None
        proc_at = db_processed or unwind_event_ts or _last_fill_ts(comp_orders)
        duration = _duration_sec(sig.received_at, proc_at)
        return ("SQUARE-OFF", False, "Incomplete leg timeout reached — Exposure automatically squared off", proc_at, duration)

    # 3. Primary Orders Full Fill (Cumulative Leg Reconciliation)
    if primary_orders:
        legs_map: dict[str, dict[str, Any]] = {}
        for o in primary_orders:
            basket = o.get("basket_id")
            leg_id = o.get("leg")
            if leg_id is not None and str(leg_id).strip() != "":
                key = f"basket:{basket}:leg:{leg_id}"
            else:
                key = f"basket:{basket}:sym:{o.get('symbol')}:{o.get('buy_sell')}"

            req_qty = float(o.get("quantity") or 0.0)
            fill_qty = float(
                o.get("fill_qty")
                if o.get("fill_qty") is not None
                else (o.get("filled_quantity") or 0.0)
            )

            if key not in legs_map:
                legs_map[key] = {
                    "req": req_qty,
                    "cum_fill": fill_qty,
                    "symbol": o.get("symbol"),
                }
            else:
                legs_map[key]["req"] = max(legs_map[key]["req"], req_qty)
                legs_map[key]["cum_fill"] += fill_qty

        all_filled = (
            len(legs_map) > 0
            and all(info["cum_fill"] + 1e-6 >= info["req"] > 0 for info in legs_map.values())
        )
        if all_filled:
            proc_at = db_processed or _last_fill_ts(primary_orders)
            duration = _duration_sec(sig.received_at, proc_at)
            return ("ACCEPTED", False, None, proc_at, duration)

        # Active Working Orders
        has_working = any(
            o.get("status") in ("SUBMITTED", "PRESUBMITTED", "PENDING", "PARTIALLY_FILLED", "RETRYING")
            for o in primary_orders
        )
        if has_working:
            return ("PROCESSING", True, "Working orders executing in IBKR", None, None)

        if any(o.get("status") in ("CANCELLED", "REJECTED", "ERROR") for o in primary_orders):
            proc_at = db_processed
            duration = _duration_sec(sig.received_at, proc_at)
            return ("REJECTED", False, "Broker leg order rejected or cancelled", proc_at, duration)

    # 4. Explicit PROCESSED status in DB
    if raw_status in ("PROCESSED", "FILLED", "SUCCESS"):
        proc_at = db_processed or _last_fill_ts(primary_orders)
        duration = _duration_sec(sig.received_at, proc_at)
        return ("ACCEPTED", False, None, proc_at, duration)

    # 5. Stale / Orphaned Signals (e.g. status == NEW but no active working orders)
    if primary_orders and any(o.get("status") in ("CANCELLED", "REJECTED", "ERROR") for o in primary_orders):
        proc_at = db_processed
        duration = _duration_sec(sig.received_at, proc_at)
        return ("REJECTED", False, "Broker leg order rejected or cancelled", proc_at, duration)

    return ("PROCESSING", True, "Awaiting broker orders", None, None)
