"""IBKR execution snapshot types and collector for reqExecutions."""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_MAX_SANE_PRICE = 1e12


def normalize_execution_side(raw: str | None) -> str:
    """Normalize IBKR broker action/side BOT/SLD to BUY/SELL."""
    if not raw:
        return "UNKNOWN"
    s = raw.strip().upper()
    if s in ("BOT", "BUY"):
        return "BUY"
    if s in ("SLD", "SELL"):
        return "SELL"
    return s


def parse_ibkr_execution_time(raw: Any) -> str:
    """Parse IBKR execution.time string into standard ISO 8601 format.

    IBKR typically formats execution.time as 'YYYYMMDD HH:MM:SS' or 'YYYYMMDD-HH:MM:SS'
    optionally followed by timezone (e.g. ' America/New_York' or ' EST').
    Falls back to current UTC ISO timestamp if empty or unparseable.
    """
    if not raw:
        return datetime.now(UTC).isoformat()
    if isinstance(raw, datetime):
        return raw.astimezone(UTC).isoformat() if raw.tzinfo else raw.replace(tzinfo=UTC).isoformat()

    s = str(raw).strip()
    # Normalize separator
    cleaned = s.replace("-", " ")
    parts = cleaned.split()
    if len(parts) >= 2:
        date_part, time_part = parts[0], parts[1]
        if len(date_part) == 8 and len(time_part) >= 8:
            try:
                dt = datetime.strptime(f"{date_part} {time_part[:8]}", "%Y%m%d %H:%M:%S").replace(tzinfo=UTC)
                return dt.isoformat()
            except ValueError:
                pass

    # If parsing as standard datetime fails, return raw string or ISO fallback
    try:
        dt = datetime.fromisoformat(s)
        return dt.astimezone(UTC).isoformat() if dt.tzinfo else dt.replace(tzinfo=UTC).isoformat()
    except ValueError:
        return s


def _safe_float(val: Any) -> float:
    try:
        v = float(val or 0.0)
        return v if math.isfinite(v) else 0.0
    except (ValueError, TypeError):
        return 0.0


def _safe_int(val: Any) -> int | None:
    if val is None or val == "":
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _clean_commission(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        v = float(raw)
        if math.isfinite(v) and 0.0 <= v < _MAX_SANE_PRICE:
            return v
    except (ValueError, TypeError):
        pass
    return None


def _clean_realized_pnl(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        v = float(raw)
        # IBKR sends DBL_MAX (~1.79e308) when realized PnL is unset
        if math.isfinite(v) and abs(v) < _MAX_SANE_PRICE:
            return v
    except (ValueError, TypeError):
        pass
    return None


@dataclass(frozen=True)
class BrokerExecutionLine:
    """Immutable snapshot line representing an IBKR Gateway execution."""

    exec_id: str
    executed_at: str
    ibkr_account: str
    symbol: str
    sec_type: str
    currency: str
    exchange: str
    con_id: int
    side: str
    quantity: float
    price: float
    cum_qty: float
    avg_price: float
    broker_order_id: int | None
    perm_id: int | None
    client_id: int | None
    commission: float | None = None
    commission_currency: str | None = None
    realized_pnl: float | None = None


@dataclass
class ExecutionSnapshotCollector:
    """Collects execution callbacks for a specific reqId until execDetailsEnd.

    Handling of Duplicate execIds and Corrections:
    -------------------------------------------------
    IBKR identifies each execution fill with a unique `execId`. Occasionally,
    IBKR may deliver duplicate `execDetails` callbacks (e.g. during connection blips)
    or send trade corrections with an existing `execId`.

    Decision:
    - We store executions in a dictionary keyed by `exec_id`.
    - An incoming `execDetails` with an already seen `exec_id` updates/replaces
      the existing line while preserving any previously associated commission/PnL.
    - If `commissionReport` arrives before `execDetails` (out of order), the commission
      is buffered in `_pending_commissions` and merged when `execDetails` arrives.
    - If `commissionReport` arrives after `execDetails`, the line in `_executions`
      is updated in-place.
    - This ensures idempotency, eliminates duplicates, and safely applies corrections.
    """

    req_id: int | None = None
    _executions: dict[str, BrokerExecutionLine] = None  # type: ignore[assignment]
    _pending_commissions: dict[str, dict[str, Any]] = None  # type: ignore[assignment]
    _done: threading.Event = None  # type: ignore[assignment]
    _lock: threading.Lock = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._executions = {}
        self._pending_commissions = {}
        self._done = threading.Event()
        self._lock = threading.Lock()

    def reset(self, req_id: int) -> None:
        """Reset collector state for a new reqExecutions request."""
        with self._lock:
            self.req_id = req_id
            self._executions.clear()
            self._pending_commissions.clear()
        self._done.clear()

    def on_exec_details(self, reqId: int, contract: Any, execution: Any) -> None:
        """Process an execDetails callback from TWSClient for the bound req_id."""
        with self._lock:
            if self.req_id is None or reqId != self.req_id:
                return

            exec_id = str(getattr(execution, "execId", "") or "").strip()
            if not exec_id:
                return

            # Check if this exec_id was already received (duplicate or correction)
            existing_line = self._executions.get(exec_id)

            # Check for commission: either buffered or already attached to existing line
            commission = None
            comm_currency = None
            realized_pnl = None

            if existing_line is not None and existing_line.commission is not None:
                commission = existing_line.commission
                comm_currency = existing_line.commission_currency
                realized_pnl = existing_line.realized_pnl
            elif exec_id in self._pending_commissions:
                pending = self._pending_commissions.pop(exec_id)
                commission = pending["commission"]
                comm_currency = pending["currency"]
                realized_pnl = pending["realized_pnl"]

            acct = str(getattr(execution, "acctNumber", "") or "").strip()
            symbol = str(getattr(contract, "symbol", "") or "").strip()
            sec_type = str(getattr(contract, "secType", "") or "").strip()
            currency = str(getattr(contract, "currency", "USD") or "USD").strip()
            exchange = str(getattr(contract, "exchange", "") or "").strip()
            con_id = int(getattr(contract, "conId", 0) or 0)
            side = normalize_execution_side(getattr(execution, "side", None))
            shares = _safe_float(getattr(execution, "shares", 0.0))
            price = _safe_float(getattr(execution, "price", 0.0))
            cum_qty = _safe_float(getattr(execution, "cumQty", 0.0))
            avg_price = _safe_float(getattr(execution, "avgPrice", 0.0))
            order_id = _safe_int(getattr(execution, "orderId", None))
            perm_id = _safe_int(getattr(execution, "permId", None))
            client_id = _safe_int(getattr(execution, "clientId", None))
            exec_time = parse_ibkr_execution_time(getattr(execution, "time", None))

            line = BrokerExecutionLine(
                exec_id=exec_id,
                executed_at=exec_time,
                ibkr_account=acct,
                symbol=symbol,
                sec_type=sec_type,
                currency=currency,
                exchange=exchange,
                con_id=con_id,
                side=side,
                quantity=shares,
                price=price,
                cum_qty=cum_qty,
                avg_price=avg_price,
                broker_order_id=order_id,
                perm_id=perm_id,
                client_id=client_id,
                commission=commission,
                commission_currency=comm_currency,
                realized_pnl=realized_pnl,
            )

            self._executions[exec_id] = line

    def on_commission_report(self, report: Any) -> None:
        """Process a commissionReport callback matching against execId."""
        exec_id = str(getattr(report, "execId", "") or "").strip()
        if not exec_id:
            return

        commission = _clean_commission(getattr(report, "commission", None))
        currency = str(getattr(report, "currency", "") or "").strip()
        raw_pnl = getattr(report, "realizedPNL", None)
        if raw_pnl is None:
            raw_pnl = getattr(report, "realizedPnl", None)
        realized_pnl = _clean_realized_pnl(raw_pnl)

        with self._lock:
            if exec_id in self._executions:
                current = self._executions[exec_id]
                updated = replace(
                    current,
                    commission=commission,
                    commission_currency=currency,
                    realized_pnl=realized_pnl,
                )
                self._executions[exec_id] = updated
            else:
                self._pending_commissions[exec_id] = {
                    "commission": commission,
                    "currency": currency,
                    "realized_pnl": realized_pnl,
                }

    def on_exec_details_end(self, reqId: int) -> None:
        """Signal completion when execDetailsEnd matches the request's reqId."""
        with self._lock:
            if self.req_id is not None and reqId == self.req_id:
                self._done.set()

    def wait(self, timeout: float) -> bool:
        """Block until execDetailsEnd is received or timeout expires."""
        return self._done.wait(timeout=timeout)

    def snapshot(self) -> list[BrokerExecutionLine]:
        """Return a snapshot list of collected broker executions."""
        with self._lock:
            # Sort executions by executed_at descending or arrival
            return list(self._executions.values())
