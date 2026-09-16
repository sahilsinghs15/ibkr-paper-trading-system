#!/usr/bin/env python3
"""Operator script: flatten live IB Gateway/TWS positions with paced MARKET closes.

Reads open positions via reqPositions, then submits the opposite MARKET order
for each line (BUY to cover shorts, SELL to close longs). Local --pace 0.2
(~5 placeOrder/sec); does not share the in-process GatewayRateLimiter.
Uses API client id 99 by default so it does not disconnect the trading app
(client id 1). Runbook: app/docs/backend-kill-switch.md.

This talks to IBKR directly. It does NOT use the app kill-switch flatten path
(that only closes Postgres OPEN rows). Arm the kill switch first if you need
to block TradingView OPENs while this runs:

    curl -X POST http://127.0.0.1:8000/api/v1/config/accounts/7/square-off

Default is a dry run. Nothing is submitted unless --apply is passed.

Paper ports only ({7497, 4002}) unless --allow-live is also passed.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_backend_dir = str(Path(__file__).resolve().parents[2])
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from ibapi.contract import Contract  # type: ignore[import-untyped]
from ibapi.order import Order as IBOrder  # type: ignore[import-untyped]

from app.broker.ibkr.tws_client import TWSClient
from app.core.config import get_settings
from app.core.logger import setup_logging
from app.oms.retry_policy import PAPER_IBKR_PORTS

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset(
    {"Filled", "Cancelled", "ApiCancelled", "Inactive", "Rejected"}
)


@dataclass
class OpenPosition:
    account: str
    symbol: str
    sec_type: str
    con_id: int
    currency: str
    quantity: float
    avg_cost: float

    @property
    def close_action(self) -> str:
        return "SELL" if self.quantity > 0 else "BUY"

    @property
    def close_qty(self) -> float:
        return abs(self.quantity)


@dataclass
class SubmittedClose:
    tws_id: int
    position: OpenPosition
    status: str = "Submitted"
    filled: float = 0.0
    remaining: float = 0.0
    avg_price: float = 0.0
    error: str | None = None


@dataclass
class FlattenListener:
    positions: list[OpenPosition] = field(default_factory=list)
    submitted: dict[int, SubmittedClose] = field(default_factory=dict)
    _pos_done: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def on_position(self, account, contract, position, avgCost) -> None:
        qty = float(position or 0)
        if abs(qty) <= 1e-9:
            return
        con_id = int(getattr(contract, "conId", 0) or 0)
        self.positions.append(
            OpenPosition(
                account=str(account),
                symbol=str(getattr(contract, "symbol", "") or ""),
                sec_type=str(getattr(contract, "secType", "") or ""),
                con_id=con_id,
                currency=str(getattr(contract, "currency", "") or "USD"),
                quantity=qty,
                avg_cost=float(avgCost or 0),
            )
        )

    def on_position_end(self) -> None:
        self._pos_done.set()

    def on_order_status(
        self,
        orderId: int,
        status: str,
        filled: float,
        remaining: float,
        avgFillPrice: float,
        permId: int,
        parentId: int,
        lastFillPrice: float,
        clientId: int,
        whyHeld: str,
        mktCapPrice: float,
    ) -> None:
        with self._lock:
            row = self.submitted.get(orderId)
            if row is None:
                return
            row.status = status
            row.filled = float(filled or 0)
            row.remaining = float(remaining or 0)
            if avgFillPrice:
                row.avg_price = avgFillPrice

    def on_error(self, reqId: int, errorCode: int, errorString: str) -> None:
        if 2000 <= errorCode < 3000:
            return
        with self._lock:
            row = self.submitted.get(reqId)
            if row is not None:
                row.error = f"{errorCode}: {errorString}"
                if errorCode == 201:
                    row.status = "Rejected"

    def wait_positions(self, timeout: float) -> bool:
        return self._pos_done.wait(timeout=timeout)

    def all_terminal(self) -> bool:
        with self._lock:
            if not self.submitted:
                return True
            return all(row.status in _TERMINAL_STATUSES for row in self.submitted.values())


def _build_close_contract(pos: OpenPosition) -> Contract:
    contract = Contract()
    if pos.con_id:
        contract.conId = pos.con_id
    contract.symbol = pos.symbol
    contract.secType = pos.sec_type or "CFD"
    contract.exchange = "SMART"
    contract.currency = pos.currency or "USD"
    return contract


def _build_close_order(pos: OpenPosition) -> IBOrder:
    order = IBOrder()
    order.action = pos.close_action
    order.totalQuantity = pos.close_qty  # pyrefly: ignore[bad-assignment]
    order.orderType = "MKT"
    order.transmit = True
    order.eTradeOnly = False
    order.firmQuoteOnly = False
    order.account = pos.account
    return order


def _filter_positions(
    rows: list[OpenPosition],
    *,
    account: str | None,
    sec_type: str | None,
) -> list[OpenPosition]:
    out: list[OpenPosition] = []
    for row in rows:
        if account and row.account != account:
            continue
        if sec_type and row.sec_type.upper() != sec_type.upper():
            continue
        out.append(row)
    out.sort(key=lambda r: (r.account, r.symbol, r.con_id))
    return out


def _print_plan(rows: list[OpenPosition]) -> None:
    print(f"\nFlatten plan: {len(rows)} position(s)\n")
    print(f"{'ACCOUNT':<12} {'SYMBOL':<8} {'TYPE':<5} {'QTY':>12} {'ACTION':<6} {'CLOSE QTY':>12} {'CONID':>12}")
    for row in rows:
        print(
            f"{row.account:<12} {row.symbol:<8} {row.sec_type:<5} {row.quantity:>12.4f} "
            f"{row.close_action:<6} {row.close_qty:>12.4f} {row.con_id:>12}"
        )


def _next_order_id(client: TWSClient) -> int:
    current = client.next_order_id
    if current is None:
        raise RuntimeError("TWS handshake did not provide nextValidId")
    client.next_order_id = current + 1
    return current


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Flatten IB Gateway/TWS open positions with paced MARKET closes."
    )
    parser.add_argument("--host", default=None, help="TWS/Gateway host (default from Settings)")
    parser.add_argument("--port", type=int, default=None, help="TWS/Gateway port (default from Settings)")
    parser.add_argument(
        "--client-id",
        type=int,
        default=99,
        help="API client id (default 99; do not use the trading app's client id 1)",
    )
    parser.add_argument(
        "--account",
        default="DUR919062",
        help="IBKR account string to flatten (default DUR919062). Empty = all accounts.",
    )
    parser.add_argument(
        "--sec-type",
        default="CFD",
        help="Only flatten this secType (default CFD). Pass ALL for every type.",
    )
    parser.add_argument(
        "--pace",
        type=float,
        default=0.2,
        help="Minimum seconds between placeOrder calls (default 0.2)",
    )
    parser.add_argument(
        "--fill-timeout",
        type=float,
        default=90.0,
        help="Seconds to wait after last submit for terminal statuses (default 90)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually submit close orders. Without this flag, dry-run only.",
    )
    parser.add_argument(
        "--allow-live",
        action="store_true",
        help="Allow ports other than paper {7497, 4002}.",
    )
    return parser.parse_args()


def run_flatten_gateway_positions(
    *,
    host: str | None = None,
    port: int | None = None,
    client_id: int = 99,
    account: str | None = None,
    sec_type: str | None = "CFD",
    pace: float = 0.2,
    fill_timeout: float = 90.0,
    apply: bool = True,
    allow_live: bool = False,
) -> dict[str, Any]:
    """Execute account-level position flatten against IB Gateway / TWS.

    Returns summary dict with count, status, and fill details.
    """
    settings = get_settings()
    target_host = host or settings.ibkr_host
    target_port = port if port is not None else settings.ibkr_port
    target_account = account.strip() if account and account.strip() else None
    target_sec_type = (
        None if sec_type and sec_type.upper() == "ALL" else (sec_type.upper() if sec_type else None)
    )

    if client_id == settings.ibkr_client_id:
        msg = f"Refusing client id {client_id}: that is the trading app socket (IBKR_CLIENT_ID). Use client id 99."
        return {"success": False, "submitted": 0, "filled": 0, "rejected": 0, "pending": 0, "positions_found": 0, "error": msg}
    if target_port not in PAPER_IBKR_PORTS and not allow_live:
        msg = f"Refusing port {target_port}: not a paper port {sorted(PAPER_IBKR_PORTS)}. Pass allow_live=True if intended."
        return {"success": False, "submitted": 0, "filled": 0, "rejected": 0, "pending": 0, "positions_found": 0, "error": msg}
    if pace < 0:
        return {"success": False, "submitted": 0, "filled": 0, "rejected": 0, "pending": 0, "positions_found": 0, "error": "pace must be >= 0"}

    listener = FlattenListener()
    client = TWSClient()
    client.register_listener(listener)
    connected = client.connect_and_start(
        host=target_host,
        port=target_port,
        client_id=client_id,
        timeout=settings.ibkr_connection_timeout,
    )
    if not connected:
        return {"success": False, "submitted": 0, "filled": 0, "rejected": 0, "pending": 0, "positions_found": 0, "error": "Could not connect to TWS/Gateway"}

    try:
        client.reqPositions()
        if not listener.wait_positions(timeout=15.0):
            pass
        try:
            client.cancelPositions()
        except Exception as exc:  # noqa: BLE001
            logger.debug("cancelPositions exception ignored: %s", exc)
        time.sleep(0.2)

        plan = _filter_positions(listener.positions, account=target_account, sec_type=target_sec_type)
        if not plan:
            return {"success": True, "submitted": 0, "filled": 0, "rejected": 0, "pending": 0, "positions_found": 0, "error": None}

        missing_conid = [p.symbol for p in plan if not p.con_id]
        if missing_conid:
            return {
                "success": False,
                "submitted": 0,
                "filled": 0,
                "rejected": 0,
                "pending": 0,
                "positions_found": len(plan),
                "error": f"Missing conId for {missing_conid}",
            }

        if not apply:
            return {"success": True, "submitted": 0, "filled": 0, "rejected": 0, "pending": 0, "positions_found": len(plan), "error": "Dry run only"}

        last_submit = 0.0
        for pos in plan:
            wait = pace - (time.monotonic() - last_submit)
            if wait > 0:
                time.sleep(wait)
            tws_id = _next_order_id(client)
            close = SubmittedClose(tws_id=tws_id, position=pos, remaining=pos.close_qty)
            listener.submitted[tws_id] = close
            client.placeOrder(tws_id, _build_close_contract(pos), _build_close_order(pos))
            last_submit = time.monotonic()

        deadline = time.monotonic() + fill_timeout
        while time.monotonic() < deadline and not listener.all_terminal():
            time.sleep(0.25)

        filled = rejected = pending = 0
        for row in listener.submitted.values():
            if row.status == "Filled":
                filled += 1
            elif row.status in {"Rejected", "Inactive", "Cancelled", "ApiCancelled"}:
                rejected += 1
            else:
                pending += 1

        is_ok = (pending == 0 and rejected == 0)
        return {
            "success": is_ok,
            "submitted": len(plan),
            "filled": filled,
            "rejected": rejected,
            "pending": pending,
            "positions_found": len(plan),
            "error": None if is_ok else f"Flatten incomplete: filled={filled}, rejected={rejected}, pending={pending}",
        }
    finally:
        client.disconnect_clean()


def main() -> int:
    args = parse_args()
    setup_logging(level="INFO", filename_prefix="flatten-gateway")
    res = run_flatten_gateway_positions(
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        account=args.account,
        sec_type=args.sec_type,
        pace=args.pace,
        fill_timeout=args.fill_timeout,
        apply=args.apply,
        allow_live=args.allow_live,
    )
    if res["error"]:
        print(f"Result error: {res['error']}", file=sys.stderr)
    print(f"Summary: positions={res['positions_found']} submitted={res['submitted']} filled={res['filled']} rejected={res['rejected']} pending={res['pending']}")
    return 0 if res["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
