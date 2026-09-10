"""Read-only snapshot of executed positions from PostgreSQL. Never writes."""

import json
import math
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.account import AccountModel
from app.db.models.basket import BasketModel
from app.db.models.event import EventLogModel
from app.db.models.execution import ExecutionModel
from app.db.models.order import OrderModel
from app.db.models.position import PositionModel
from app.db.models.signal import SignalJobModel, SignalModel
from app.services.account_reject_reason import (
    format_account_reject_reason,
    has_account_prefixed_segments,
    is_generic_reject_reason,
    resolve_reject_source,
    scope_reject_reason_for_account,
)
from app.services.risk_exit_rules import pair_entry_gross_notional

RISK_OPEN = "OPEN"
RISK_CLOSED = "CLOSED"


def _resolve_scoped_reject(
    sig_reject: str | None,
    job_reject: str | None,
    *,
    ibkr_account: str | None,
    account_id: int | None,
) -> str | None:
    """Pick the best reject text and scope it to the open account."""
    best = resolve_reject_source(job_reject, sig_reject)
    if not best:
        return None
    raw_for_scope = best
    if job_reject and best == job_reject and not is_generic_reject_reason(job_reject):
        raw_for_scope = format_account_reject_reason(
            ibkr_account,
            best,
            account_id=account_id,
        ) or best
    return scope_reject_reason_for_account(
        raw_for_scope,
        ibkr_account=ibkr_account,
        account_id=account_id,
    )


class _SignalRejectView:
    """Lightweight SignalModel view with an overridden reject_reason."""

    def __init__(self, base: SignalModel, reject_reason: str | None) -> None:
        self._base = base
        self.reject_reason = reject_reason

    def __getattr__(self, name: str):
        return getattr(self._base, name)



def _norm_ibkr(value: str | None) -> str | None:
    if value is None:
        return None
    s = value.strip().upper()
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
    return target_acc_id is not None and p_str.isdigit() and int(p_str) == target_acc_id


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
    for b_row in baskets:
        if b_row.action.upper() == "CLOSE" and b_row.state in ("EXECUTING", "UNWINDING"):
            return True
    for o_row in orders:
        if o_row.is_compensation:
            continue
        if ":CLOSE" in (o_row.internal_order_id or "") and o_row.status not in ("CANCELLED", "REJECTED"):
            return True
    return False


def execution_payload(ex: ExecutionModel) -> dict[str, Any]:
    """JSON dict for one IBKR fill nested under an order."""
    return {
        "id": ex.id,
        "exec_id": ex.exec_id,
        "symbol": ex.symbol,
        "side": ex.side,
        "quantity": float(ex.quantity) if ex.quantity is not None else 0.0,
        "price": float(ex.price) if ex.price is not None else 0.0,
        "commission": float(ex.commission) if ex.commission is not None else None,
        "realized_pnl": float(ex.realized_pnl) if ex.realized_pnl is not None else None,
        "executed_at": (
            ex.executed_at.isoformat()
            if ex.executed_at
            else ex.created_at.isoformat()
            if ex.created_at
            else None
        ),
    }


def order_payload(
    order: OrderModel, executions: list[ExecutionModel]
) -> dict[str, Any]:
    """JSON dict for one ledger order with nested fills."""
    m_execs = sorted(executions, key=lambda x: x.id)
    return {
        "id": order.id,
        "internal_order_id": order.internal_order_id,
        "basket_id": order.basket_id,
        "leg": order.leg,
        "symbol": order.symbol,
        "buy_sell": order.buy_sell,
        "quantity": float(order.quantity) if order.quantity is not None else 0.0,
        "fill_qty": float(order.fill_qty) if order.fill_qty is not None else 0.0,
        "fill_price": float(order.fill_price) if order.fill_price is not None else None,
        "status": order.status,
        "broker_order_id": order.broker_order_id,
        "is_compensation": order.is_compensation,
        "compensation_of_internal_order_id": order.compensation_of_internal_order_id,
        "filled_at": order.filled_at.isoformat() if order.filled_at else None,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "executions": [execution_payload(ex) for ex in m_execs],
    }


def event_payload(ev: EventLogModel) -> dict[str, Any]:
    """JSON dict for one event_log row."""
    return {
        "id": ev.id,
        "kind": ev.kind,
        "process": ev.process,
        "ts": ev.ts.isoformat() if ev.ts else None,
        "detail": ev.detail or {},
        "order_id": ev.order_id,
        "basket_id": ev.basket_id,
        "signal_id": ev.signal_id,
    }


def basket_payload(row: BasketModel) -> dict[str, Any]:
    return {
        "id": row.id,
        "action": row.action,
        "state": row.state,
        "intended_leg_count": row.intended_leg_count,
        "recovery_status": row.recovery_status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


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
    live_pnl = None
    if position.risk_state == RISK_OPEN and position.live_pnl != Decimal(0):
        live_pnl = _dec(_quantize_pnl(position.live_pnl))
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
    return [(row[0], row[1]) for row in result.all()]


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
    return [(row[0], row[1]) for row in result.all()]


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
    row = result.first()
    return (row[0], row[1]) if row is not None else None


async def load_pair_detail(
    session: AsyncSession, account_id: int, trade_id: str
) -> dict[str, Any] | None:
    """Full history payload for one (account_id, trade_id) pair, any risk_state."""
    loaded = await load_position_with_account(session, account_id, trade_id)
    if loaded is None:
        return None
    position, account = loaded
    key = (account_id, trade_id)
    baskets_map = await load_baskets(session, {key})
    orders_map = await load_orders(session, {key})
    baskets = baskets_map.get(key, [])
    orders = sorted(orders_map.get(key, []), key=lambda o: o.id)

    order_ids = [o.id for o in orders if o.id]
    internal_ids = [o.internal_order_id for o in orders if o.internal_order_id]
    all_executions: list[ExecutionModel] = []
    exec_filters = []
    if order_ids:
        exec_filters.append(ExecutionModel.order_id.in_(order_ids))
    if internal_ids:
        exec_filters.append(ExecutionModel.internal_order_id.in_(internal_ids))
    if exec_filters:
        exec_stmt = select(ExecutionModel).where(or_(*exec_filters))
        all_executions = list((await session.execute(exec_stmt)).scalars().all())
    execs_by_order_id: dict[int, list[ExecutionModel]] = {}
    execs_by_internal_id: dict[str, list[ExecutionModel]] = {}
    for ex in all_executions:
        if ex.order_id:
            execs_by_order_id.setdefault(ex.order_id, []).append(ex)
        if ex.internal_order_id:
            execs_by_internal_id.setdefault(ex.internal_order_id, []).append(ex)

    orders_payload = []
    for o in orders:
        m_execs = execs_by_order_id.get(o.id) or (
            execs_by_internal_id.get(o.internal_order_id) if o.internal_order_id else []
        ) or []
        orders_payload.append(order_payload(o, m_execs))

    basket_ids = [b.id for b in baskets if b.id]
    event_filters: list[Any] = []
    if order_ids:
        event_filters.append(EventLogModel.order_id.in_(order_ids))
    if basket_ids:
        event_filters.append(EventLogModel.basket_id.in_(basket_ids))
    event_filters.append(EventLogModel.detail.contains({"trade_id": trade_id}))
    events = list(
        (
            await session.execute(
                select(EventLogModel).where(or_(*event_filters)).order_by(EventLogModel.id)
            )
        ).scalars().all()
    )
    seen: set[int] = set()
    events_payload = []
    for ev in events:
        if ev.id in seen:
            continue
        seen.add(ev.id)
        events_payload.append(event_payload(ev))

    notional = pair_entry_gross_notional(
        leg_a_signed_qty=position.leg_a_signed_qty,
        leg_a_entry_mark=position.leg_a_entry_mark,
        leg_b_signed_qty=position.leg_b_signed_qty,
        leg_b_entry_mark=position.leg_b_entry_mark,
    )
    return {
        "position": {
            "account_id": position.account_id,
            "ibkr_account": account.ibkr_account,
            "account_name": account.name,
            "trade_id": position.trade_id,
            "strategy_id": position.strategy_id,
            "risk_state": position.risk_state,
            "leg_a_symbol": position.leg_a_symbol,
            "leg_a_signed_qty": _dec(position.leg_a_signed_qty),
            "leg_a_entry_mark": _dec(position.leg_a_entry_mark),
            "leg_a_instrument_type": position.leg_a_instrument_type,
            "leg_b_symbol": position.leg_b_symbol,
            "leg_b_signed_qty": _dec(position.leg_b_signed_qty),
            "leg_b_entry_mark": _dec(position.leg_b_entry_mark),
            "leg_b_instrument_type": position.leg_b_instrument_type,
            "live_pnl": _dec(position.live_pnl),
            "realised_pnl": _dec(position.realised_pnl),
            "commission": _dec(position.commission),
            "opened_at": position.opened_at.isoformat() if position.opened_at else None,
            "closed_at": position.closed_at.isoformat() if position.closed_at else None,
            "exit_reason": position.exit_reason,
            "entry_gross_notional": _dec(notional),
        },
        "exits": {
            "target": _dec(position.target),
            "stop": _dec(position.stop),
            "time_limit": position.time_limit,
            "target_unit": getattr(position, "target_unit", None) or "ABSOLUTE",
            "stop_unit": getattr(position, "stop_unit", None) or "ABSOLUTE",
            "exit_automation_enabled": bool(
                getattr(position, "exit_automation_enabled", False)
            ),
        },
        "orders": orders_payload,
        "baskets": [basket_payload(b) for b in baskets],
        "events": events_payload,
    }


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
    job_error_by_sig_scope: dict[tuple[str, str], str] = {}

    job_stmt = select(
        SignalJobModel.signal_id,
        SignalJobModel.account_scope,
        SignalJobModel.last_error,
    )
    job_res = (await session.execute(job_stmt)).all()
    for sig_str_id, acc_scope, last_error in job_res:
        if not acc_scope:
            continue
        scope_str = str(acc_scope).strip()
        job_scopes_by_sig.setdefault(str(sig_str_id), []).append(scope_str)
        if last_error and str(last_error).strip():
            job_error_by_sig_scope[(str(sig_str_id), scope_str)] = str(last_error).strip()
            job_error_by_sig_scope[(str(sig_str_id), scope_str.upper())] = str(last_error).strip()

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
        for sig_str_id, scopes in job_scopes_by_sig.items():
            for scope_str in scopes:
                if (target_acc_id and scope_str == str(target_acc_id)) or (
                    target_ibkr_acc and scope_str.upper() == target_ibkr_acc.upper()
                ):
                    s_stmt = select(SignalModel.id).where(SignalModel.signal_id == sig_str_id)
                    s_res = (await session.execute(s_stmt)).scalars().all()
                    matched_sig_ids.update(s_res)
                    break

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
        all_executions = list((await session.execute(exec_stmt)).scalars().all())

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
            for scope_str in job_scopes_by_sig.get(sig.signal_id, []):
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
            m_execs = execs_by_order_id.get(o.id) or (
                execs_by_internal_id.get(o.internal_order_id) if o.internal_order_id else []
            ) or []
            orders_payload.append(order_payload(o, m_execs))

        matched_events = events_by_sig_id.get(sig.id, [])
        if target_acc_id is not None or target_ibkr_acc:
            matched_events = [
                ev for ev in matched_events
                if ev.order_id is None or ev.order_id in scoped_order_ids
            ]
        matched_events = sorted(matched_events, key=lambda x: x.id)

        events_payload = [event_payload(ev) for ev in matched_events]

        job_reject: str | None = None
        if sig.signal_id:
            sid = sig.signal_id
            for key in (
                str(sig_acc_id) if sig_acc_id is not None else None,
                str(target_acc_id) if target_acc_id is not None else None,
                (sig_ibkr_acc or "").strip().upper() or None,
                (target_ibkr_canonical or "").strip().upper() if target_ibkr_canonical else None,
            ):
                if not key:
                    continue
                job_reject = job_error_by_sig_scope.get((sid, key)) or job_error_by_sig_scope.get(
                    (sid, key)
                )
                if job_reject:
                    break

        scoped_reject = _resolve_scoped_reject(
            sig.reject_reason,
            job_reject,
            ibkr_account=sig_ibkr_acc or target_ibkr_canonical,
            account_id=sig_acc_id if sig_acc_id is not None else target_acc_id,
        )
        # Sibling-only fan-out rejection: this account is not mentioned and has
        # no local orders / job error — do not surface it on this account's dashboard.
        if (
            (target_acc_id is not None or target_ibkr_acc)
            and not matched_orders
            and not job_reject
            and has_account_prefixed_segments(sig.reject_reason)
            and scoped_reject is None
        ):
            continue

        sig_for_reconcile: SignalModel | _SignalRejectView = sig
        if (
            target_acc_id is not None
            or target_ibkr_acc
            or scoped_reject != sig.reject_reason
            or job_reject
        ):
            sig_for_reconcile = _SignalRejectView(sig, scoped_reject)

        c_status, is_active, rec_reason, calc_proc_at, duration_sec = reconcile_signal_status(
            sig_for_reconcile, orders_payload, events_payload  # type: ignore[arg-type]
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

        # Re-scope after late account resolution (unfiltered / for_watch path).
        if sig_ibkr_acc or sig_acc_id is not None:
            if not job_reject and sig.signal_id:
                sid = sig.signal_id
                for key in (
                    str(sig_acc_id) if sig_acc_id is not None else None,
                    (sig_ibkr_acc or "").strip().upper() or None,
                ):
                    if not key:
                        continue
                    job_reject = job_error_by_sig_scope.get((sid, key))
                    if job_reject:
                        break
            scoped_reject = _resolve_scoped_reject(
                sig.reject_reason,
                job_reject,
                ibkr_account=sig_ibkr_acc,
                account_id=sig_acc_id,
            )
            if scoped_reject != getattr(sig_for_reconcile, "reject_reason", None):
                sig_for_reconcile = _SignalRejectView(sig, scoped_reject)
                c_status, is_active, rec_reason, calc_proc_at, duration_sec = (
                    reconcile_signal_status(
                        sig_for_reconcile, orders_payload, events_payload  # type: ignore[arg-type]
                    )
                )

        if target_ibkr_acc and sig_ibkr_acc and _norm_ibkr(sig_ibkr_acc) != target_ibkr_acc:
            continue
        if target_acc_id is not None and sig_acc_id is not None and sig_acc_id != target_acc_id:
            continue

        display_reject = scoped_reject if scoped_reject is not None else (
            None if has_account_prefixed_segments(sig.reject_reason) else sig.reject_reason
        )
        if display_reject is None and rec_reason and not has_account_prefixed_segments(rec_reason):
            display_reject = rec_reason

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
                "reject_reason": display_reject or rec_reason,
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


def _total_fill_qty(orders: list[dict[str, Any]]) -> float:
    total = 0.0
    for o in orders:
        fill_qty = o.get("fill_qty")
        if fill_qty is not None:
            total += float(fill_qty)
        else:
            total += float(o.get("filled_quantity") or 0.0)
    return total


def reconcile_signal_status(
    sig: SignalModel,
    orders: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> tuple[str, bool, str | None, str | None, float | None]:
    """Reconcile raw database status to canonical_status, is_active_processing, reason, processed_at, duration_sec."""
    raw_status = (sig.status or "").upper()
    reject_reason = sig.reject_reason
    db_processed = sig.processed_at.isoformat() if sig.processed_at else None

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
        primary_fill = _total_fill_qty(primary_orders)
        comp_fill = _total_fill_qty(comp_orders)
        if primary_fill > 0 or comp_fill > 0:
            unwind_event_ts = unwind_event.get("ts") if unwind_event else None
            proc_at = db_processed or unwind_event_ts or _last_fill_ts(comp_orders)
            duration = _duration_sec(sig.received_at, proc_at)
            return (
                "SQUARE-OFF",
                False,
                "Incomplete leg timeout reached — Exposure automatically squared off",
                proc_at,
                duration,
            )

    # Account-scoped fills beat a shared signal-row REJECTED from a sibling account.
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
            fill_raw = o.get("fill_qty")
            if fill_raw is None:
                fill_raw = o.get("filled_quantity")
            fill_qty = float(fill_raw or 0.0)

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

        has_working = any(
            o.get("status") in ("SUBMITTED", "PRESUBMITTED", "PENDING", "PARTIALLY_FILLED", "RETRYING")
            for o in primary_orders
        )
        if has_working:
            return ("PROCESSING", True, "Working orders executing in IBKR", None, None)

        if any(o.get("status") in ("CANCELLED", "REJECTED", "ERROR") for o in primary_orders):
            proc_at = db_processed
            duration = _duration_sec(sig.received_at, proc_at)
            rec = reject_reason or "Broker leg order rejected or cancelled"
            return ("REJECTED", False, rec, proc_at, duration)

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

    # 4. Explicit PROCESSED status in DB
    if raw_status in ("PROCESSED", "FILLED", "SUCCESS"):
        proc_at = db_processed or _last_fill_ts(primary_orders)
        duration = _duration_sec(sig.received_at, proc_at)
        return ("ACCEPTED", False, None, proc_at, duration)

    # 5. Stale / Orphaned Signals (e.g. status == NEW but no active working orders)
    if primary_orders and any(o.get("status") in ("CANCELLED", "REJECTED", "ERROR") for o in primary_orders):
        proc_at = db_processed
        duration = _duration_sec(sig.received_at, proc_at)
        rec = reject_reason or "Broker leg order rejected or cancelled"
        return ("REJECTED", False, rec, proc_at, duration)

    return ("PROCESSING", True, "Awaiting broker orders", None, None)


STATUS_FILTER_MAP: dict[str, list[str]] = {
    "QUEUED": ["QUEUED", "RECEIVED"],
    "RECEIVED": ["QUEUED", "RECEIVED"],
    "PROCESSING": ["CLAIMED", "PROCESSING"],
    "CLAIMED": ["CLAIMED", "PROCESSING"],
    "COMPLETED": ["COMPLETED"],
    "REJECTED": ["REJECTED"],
    "FAILED": ["FAILED"],
    "DEFERRED": ["DEFERRED_RED_ZONE", "DEFERRED"],
    "DEFERRED_RED_ZONE": ["DEFERRED_RED_ZONE", "DEFERRED"],
    "RECOVERY": ["RECOVERY_REQUIRED", "RECOVERY"],
    "RECOVERY_REQUIRED": ["RECOVERY_REQUIRED", "RECOVERY"],
    "DEAD_LETTER": ["DEAD_LETTER"],
    "DEAD LETTER": ["DEAD_LETTER"],
}


def _ingest_job_payload(
    job: SignalJobModel,
    acc_by_id: dict[int, AccountModel],
    acc_by_ibkr: dict[str, AccountModel],
) -> dict[str, Any]:
    capture = job.capture_data if isinstance(job.capture_data, dict) else {}

    # parsed_json: prefer capture_data.parsed_json when dict, otherwise raw_payload when dict, else {}
    if isinstance(capture.get("parsed_json"), dict):
        parsed_json = capture["parsed_json"]
    elif isinstance(job.raw_payload, dict):
        parsed_json = job.raw_payload
    else:
        parsed_json = {}

    # raw_body: prefer capture_data.raw_body when string, otherwise reconstruct from parsed_json
    if isinstance(capture.get("raw_body"), str):
        raw_body = capture["raw_body"]
    else:
        raw_body = json.dumps(parsed_json) if parsed_json else ""

    # metadata: must be a dict or {}
    meta_raw = capture.get("metadata")
    metadata = meta_raw if isinstance(meta_raw, dict) else {}

    # action: extracted from parsed_json or raw_payload
    action_val = parsed_json.get("action") or (
        job.raw_payload.get("action") if isinstance(job.raw_payload, dict) else None
    )
    action = str(action_val).upper() if action_val else "UNKNOWN"

    # Account resolution:
    # 1. If account_scope is numeric and matches AccountModel.id: resolve account_id and ibkr_account.
    # 2. Otherwise attempt case-insensitive IBKR account lookup.
    # 3. If neither resolves: preserve account_scope as ibkr_account fallback.
    acc_id: int | None = None
    ibkr_acc: str | None = None
    if job.account_scope:
        scope_str = job.account_scope.strip()
        if scope_str.isdigit() and int(scope_str) in acc_by_id:
            acc = acc_by_id[int(scope_str)]
            acc_id = acc.id
            ibkr_acc = acc.ibkr_account
        elif scope_str.upper() in acc_by_ibkr:
            acc = acc_by_ibkr[scope_str.upper()]
            acc_id = acc.id
            ibkr_acc = acc.ibkr_account
        else:
            ibkr_acc = scope_str

    return {
        "job_id": str(job.job_id),
        "signal_id": job.signal_id,
        "trade_id": job.trade_id,
        "strategy_id": job.strategy_id,
        "action": action,
        "status": job.status,
        "account_scope": job.account_scope,
        "account_id": acc_id,
        "ibkr_account": ibkr_acc,
        "correlation_id": job.correlation_id,
        "idempotency_key": job.idempotency_key,
        "attempt_count": job.attempt_count,
        "max_attempts": job.max_attempts,
        "last_error": job.last_error,
        "deferral_reason": job.deferral_reason,
        "received_at": job.received_at.isoformat() if job.received_at else None,
        "queued_at": job.queued_at.isoformat() if job.queued_at else None,
        "claimed_at": job.claimed_at.isoformat() if job.claimed_at else None,
        "processing_started_at": job.processing_started_at.isoformat() if job.processing_started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "raw_body": raw_body,
        "parsed_json": parsed_json,
        "metadata": metadata,
    }


async def load_ingest_jobs(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 50,
    status_filter: str | None = None,
    account_id: int | None = None,
    ibkr_account: str | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    eff_page = max(1, page)
    eff_page_size = min(max(1, page_size), 200)

    empty_counts = {
        "all": 0,
        "total": 0,
        "queued": 0,
        "processing": 0,
        "completed": 0,
        "rejected": 0,
        "failed": 0,
        "deferred": 0,
        "recovery": 0,
        "dead_letter": 0,
    }

    if session is None or not hasattr(session, "execute"):
        return {
            "jobs": [],
            "page": eff_page,
            "page_size": eff_page_size,
            "total": 0,
            "total_pages": 1,
            "counts": empty_counts,
        }

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

    base_clauses: list[Any] = []

    # ACCOUNT FILTER:
    # When account_id or ibkr_account is supplied:
    # account_scope == str(account_id) OR case-insensitive account_scope == IBKR account string.
    # Do NOT add account_scope IS NULL to the filtered account path.
    # When admin requests an unscoped/global list: include all rows, including NULL account_scope.
    if target_acc_id is not None or target_ibkr_acc:
        account_or_clauses = []
        if target_acc_id is not None:
            account_or_clauses.append(SignalJobModel.account_scope == str(target_acc_id))
        if target_ibkr_acc:
            account_or_clauses.append(
                func.upper(SignalJobModel.account_scope) == target_ibkr_acc.upper()
            )
        base_clauses.append(or_(*account_or_clauses))

    # SEARCH FILTER:
    # Use OR/ILIKE across: signal_id, trade_id, correlation_id, strategy_id
    if search and search.strip():
        term = f"%{search.strip()}%"
        search_clause = or_(
            SignalJobModel.signal_id.ilike(term),
            SignalJobModel.trade_id.ilike(term),
            SignalJobModel.correlation_id.ilike(term),
            SignalJobModel.strategy_id.ilike(term),
        )
        base_clauses.append(search_clause)

    # 1. Grouped status histogram using base filters (WITHOUT status filter)
    count_stmt = (
        select(SignalJobModel.status, func.count(SignalJobModel.job_id))
        .where(*base_clauses)
        .group_by(SignalJobModel.status)
    )
    count_res = (await session.execute(count_stmt)).all()
    raw_counts: dict[str, int] = {}
    for st, cnt in count_res:
        if st:
            raw_counts[str(st).strip().upper()] = int(cnt)

    c_queued = raw_counts.get("QUEUED", 0) + raw_counts.get("RECEIVED", 0)
    c_processing = raw_counts.get("CLAIMED", 0) + raw_counts.get("PROCESSING", 0)
    c_completed = raw_counts.get("COMPLETED", 0)
    c_rejected = raw_counts.get("REJECTED", 0)
    c_failed = raw_counts.get("FAILED", 0)
    c_deferred = raw_counts.get("DEFERRED_RED_ZONE", 0) + raw_counts.get("DEFERRED", 0)
    c_recovery = raw_counts.get("RECOVERY_REQUIRED", 0) + raw_counts.get("RECOVERY", 0)
    c_dead_letter = raw_counts.get("DEAD_LETTER", 0)
    total_unfiltered = sum(raw_counts.values())

    counts = {
        "all": total_unfiltered,
        "total": total_unfiltered,
        "queued": c_queued,
        "processing": c_processing,
        "completed": c_completed,
        "rejected": c_rejected,
        "failed": c_failed,
        "deferred": c_deferred,
        "recovery": c_recovery,
        "dead_letter": c_dead_letter,
    }

    # 2. STATUS FILTER:
    # Ignore if unset or ALL. Otherwise compare uppercase status.
    all_clauses = list(base_clauses)
    clean_status = status_filter.strip().upper() if status_filter else None
    if clean_status and clean_status != "ALL":
        if clean_status in STATUS_FILTER_MAP:
            all_clauses.append(SignalJobModel.status.in_(STATUS_FILTER_MAP[clean_status]))
        else:
            all_clauses.append(func.upper(SignalJobModel.status) == clean_status)

    # 3. Total matching filters (including status filter)
    total_stmt = select(func.count(SignalJobModel.job_id)).where(*all_clauses)
    total = (await session.execute(total_stmt)).scalar() or 0

    # 4. Paginated SELECT
    offset = (eff_page - 1) * eff_page_size
    items_stmt = (
        select(SignalJobModel)
        .where(*all_clauses)
        .order_by(SignalJobModel.received_at.desc(), SignalJobModel.job_id.desc())
        .offset(offset)
        .limit(eff_page_size)
    )
    job_rows = (await session.execute(items_stmt)).scalars().all()

    jobs = [_ingest_job_payload(job, acc_by_id, acc_by_ibkr) for job in job_rows]
    total_pages = max(1, math.ceil(total / eff_page_size))

    return {
        "jobs": jobs,
        "page": eff_page,
        "page_size": eff_page_size,
        "total": total,
        "total_pages": total_pages,
        "counts": counts,
    }

