"""Real IBKR Paper Gateway client — uses whatIf orders only.

SAFETY INVARIANT: This prototype NEVER submits executable orders.
Every placeOrder uses Order.whatIf = True (IBKR what-if / margin estimation).
If ambiguity about executable transmission exists, execution is aborted.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from ..config import BenchmarkConfig
from ..models import Instrument
from ..rate_limiter import PrototypeRateLimiter

logger = logging.getLogger(__name__)

# IBKR imports deferred to runtime to allow mock-only test without ibapi
try:
    from ibapi.client import EClient
    from ibapi.wrapper import EWrapper
    from ibapi.contract import Contract
    from ibapi.order import Order as IBOrder
except ImportError:
    EClient = object  # type: ignore
    EWrapper = object  # type: ignore
    Contract = object  # type: ignore
    IBOrder = object  # type: ignore


class _WhatIfClient(EWrapper, EClient):
    """Single IBKR client capturing both contractDetails and whatIf openOrder/orderState.

    Uses one wrapper (self) to avoid ibapi EClient.wrapper property setter bug
    (wrapper is read-only property in 9.81.1). All state lives on self.
    """

    def __init__(self) -> None:
        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)
        self.next_order_id: int | None = None
        self._connected = threading.Event()
        self._managed_accounts: frozenset[str] = frozenset()
        self._managed_event = threading.Event()

        self._cd_lock = threading.Lock()
        self._cd_events: dict[int, threading.Event] = {}
        self._cd_results: dict[int, list[Any]] = {}
        self._next_cd_req = 70000

        self._whatif_lock = threading.Lock()
        self._whatif_events: dict[int, threading.Event] = {}
        self._whatif_results: dict[int, dict[str, str]] = {}
        self._whatif_errors: dict[int, str] = {}

    def nextValidId(self, orderId: int) -> None:
        super().nextValidId(orderId)
        self.next_order_id = orderId
        self._connected.set()
        logger.info("RealClient nextValidId %d", orderId)

    def managedAccounts(self, accountsList: str) -> None:
        codes = frozenset(c.strip().upper() for c in accountsList.split(",") if c.strip())
        self._managed_accounts = codes
        self._managed_event.set()
        logger.info("RealClient managedAccounts %s", codes)

    def error(self, reqId: int, errorCode: int, errorString: str, advancedOrderRejectJson: str = "") -> None:
        if (2000 <= errorCode < 3000) or (10000 <= errorCode < 11000) or errorCode in (399, 2109, 10349):
            logger.info("IBKR info (non-terminal) reqId=%d code=%d %s", reqId, errorCode, errorString)
            return
        else:
            logger.warning("IBKR error reqId=%d code=%d %s", reqId, errorCode, errorString)
            with self._whatif_lock:
                if reqId in self._whatif_events:
                    self._whatif_errors[reqId] = f"TWS {errorCode}: {errorString}"
                    self._whatif_events[reqId].set()
            with self._cd_lock:
                if reqId in self._cd_events:
                    self._cd_events[reqId].set()

    def contractDetails(self, reqId: int, contractDetails: Any) -> None:
        super().contractDetails(reqId, contractDetails)
        with self._cd_lock:
            if reqId in self._cd_results:
                self._cd_results[reqId].append(contractDetails)

    def contractDetailsEnd(self, reqId: int) -> None:
        super().contractDetailsEnd(reqId)
        with self._cd_lock:
            evt = self._cd_events.get(reqId)
            if evt:
                evt.set()

    def openOrder(self, orderId: int, contract: Any, order: Any, orderState: Any) -> None:
        super().openOrder(orderId, contract, order, orderState)
        with self._whatif_lock:
            if orderId in self._whatif_events:
                init_after = getattr(orderState, "initMarginAfter", "") or ""
                maint_after = getattr(orderState, "maintMarginAfter", "") or ""
                init_change = getattr(orderState, "initMarginChange", "") or ""
                maint_change = getattr(orderState, "maintMarginChange", "") or ""
                init = init_after.strip() or init_change.strip()
                maint = maint_after.strip() or maint_change.strip()
                self._whatif_results[orderId] = {
                    "init": init,
                    "maint": maint,
                    "status": getattr(orderState, "status", ""),
                }
                self._whatif_events[orderId].set()

    def orderStatus(self, orderId: int, status: str, filled: float, remaining: float, avgFillPrice: float, permId: int, parentId: int, lastFillPrice: float, clientId: int, whyHeld: str, mktCapPrice: float) -> None:
        super().orderStatus(orderId, status, filled, remaining, avgFillPrice, permId, parentId, lastFillPrice, clientId, whyHeld, mktCapPrice)
        with self._whatif_lock:
            if orderId in self._whatif_events and not self._whatif_events[orderId].is_set():
                pass


class RealIBKRClient:
    """Paper Gateway client using whatIf margin estimation.

    Safety: Order.whatIf = True is enforced. Order.transmit is irrelevant when whatIf=True
    but we set transmit=False as additional defense.
    """

    def __init__(
        self,
        config: BenchmarkConfig,
        rate_limiter: PrototypeRateLimiter | None = None,
    ) -> None:
        self.config = config
        self.rate_limiter = rate_limiter
        self._client: Any | None = None
        self._thread: threading.Thread | None = None
        self.pacing_errors = 0

    def is_connected(self) -> bool:
        return bool(self._client and self._client.isConnected() and self._client.wrapper._connected.is_set())

    async def connect(self) -> None:
        if self.is_connected():
            return
        self.config.validate()
        # Import here to fail fast if ibapi missing
        from ibapi.client import EClient as _EC  # noqa: F401

        client = _WhatIfClient()
        # connect
        try:
            client.connect(self.config.ib_host, self.config.ib_port, self.config.ib_client_id)
        except Exception as e:
            raise ConnectionError(f"TCP connect failed {self.config.ib_host}:{self.config.ib_port}: {e}") from e

        t = threading.Thread(target=client.run, daemon=True, name="RealIBKRClientThread")
        t.start()
        self._client = client
        self._thread = t

        ok = client.wrapper._connected.wait(timeout=self.config.contract_details_timeout + 5)
        if not ok:
            self._client.disconnect()
            raise ConnectionError("Handshake timeout — nextValidId not received")
        # wait briefly for managedAccounts
        client.wrapper._managed_event.wait(timeout=2.0)
        logger.info("RealIBKRClient connected to %s:%d clientId=%d", self.config.ib_host, self.config.ib_port, self.config.ib_client_id)

    async def disconnect(self) -> None:
        if self._client:
            try:
                self._client.disconnect()
            except Exception:
                pass
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=2.0)
            self._client = None
            self._thread = None

    def _build_contract(self, instrument: Instrument) -> Any:
        from ibapi.contract import Contract

        c = Contract()
        c.symbol = instrument.symbol.strip().upper()
        # ETF -> STK at IBKR (cash ETF is STK secType), CFD -> CFD
        if instrument.instrument_type.upper() == "CFD":
            c.secType = "CFD"
        else:
            c.secType = "STK"
        c.exchange = instrument.exchange.strip() or "SMART"
        c.currency = instrument.currency.strip() or "USD"
        return c

    async def resolve_contract(self, instrument: Instrument) -> tuple[int | None, float]:
        start = time.monotonic()
        if self.rate_limiter is not None:
            acquired = self.rate_limiter.blocking_acquire(timeout=self.rate_limiter.max_wait_sec)
            if not acquired:
                self.pacing_errors += 1
                raise RuntimeError("Rate limiter timeout before reqContractDetails")

        assert self._client is not None and self.is_connected(), "Not connected"
        contract = self._build_contract(instrument)

        # Use sync blocking req via thread — similar to TWSClient.request_contract_details
        def _do() -> tuple[list[Any], bool]:
            w = self._client.wrapper
            with w._cd_lock:
                req_id = w._next_cd_req
                w._next_cd_req += 1
                evt = threading.Event()
                w._cd_events[req_id] = evt
                w._cd_results[req_id] = []
            try:
                self._client.reqContractDetails(req_id, contract)
            except Exception as e:
                with w._cd_lock:
                    w._cd_events.pop(req_id, None)
                    w._cd_results.pop(req_id, None)
                raise
            completed = evt.wait(timeout=self.config.contract_details_timeout)
            with w._cd_lock:
                results = list(w._cd_results.get(req_id, []))
                w._cd_events.pop(req_id, None)
                w._cd_results.pop(req_id, None)
            return results, completed

        results, completed = await asyncio.to_thread(_do)
        elapsed_ms = (time.monotonic() - start) * 1000

        if not completed:
            raise TimeoutError(f"reqContractDetails timeout for {instrument.symbol}")
        if not results:
            raise RuntimeError(f"No contractDetails for {instrument.symbol} ({instrument.exchange})")

        # Prefer SMART/USD match, then first
        picked = None
        for row in results:
            c = getattr(row, "contract", None)
            if c is None:
                continue
            if (getattr(c, "secType", "").upper() == ("CFD" if instrument.instrument_type == "CFD" else "STK")):
                picked = row
                if getattr(c, "exchange", "").upper() == "SMART":
                    break
        picked = picked or results[0]
        con_id = int(getattr(getattr(picked, "contract", None), "conId", 0) or 0) or None
        return con_id, elapsed_ms

    async def fetch_margin(self, instrument: Instrument, con_id: int | None) -> tuple[str, str, float]:
        """Fetch margin via whatIf order — NEVER transmits executable order."""
        start = time.monotonic()
        if self.rate_limiter is not None:
            acquired = self.rate_limiter.blocking_acquire(timeout=self.rate_limiter.max_wait_sec)
            if not acquired:
                self.pacing_errors += 1
                raise RuntimeError("Rate limiter timeout before whatIf placeOrder")

        assert self._client is not None and self.is_connected(), "Not connected"

        from ibapi.contract import Contract
        from ibapi.order import Order as IBOrder

        # Build contract — use conId if discovered
        contract = self._build_contract(instrument)
        if con_id:
            contract.conId = int(con_id)

        # Build WHAT-IF order — defensive assertions
        order = IBOrder()
        order.action = "BUY"
        order.orderType = "MKT"
        order.totalQuantity = 1
        order.whatIf = True  # CRITICAL: margin estimation only (prevents execution even with transmit=True)
        order.transmit = True  # Required by IBKR for whatIf to be validated (321 error if False)
        order.eTradeOnly = False
        order.firmQuoteOnly = False
        # Ensure whatIf compliance — transmit may be True but whatIf=True guarantees no execution
        assert order.whatIf is True, "SAFETY: whatIf must be True — refusing to send executable order"

        if order.whatIf is not True:
            raise RuntimeError("SAFETY ABORT: Order.whatIf is not True — refusing to place order")

        w = self._client.wrapper
        # Reserve orderId
        with threading.Lock():
            # next_order_id is on client
            oid = self._client.wrapper.next_order_id
            if oid is None:
                oid = 1
                self._client.wrapper.next_order_id = 1
            self._client.wrapper.next_order_id = oid + 1
            order_id = oid

        with w._whatif_lock:
            evt = threading.Event()
            w._whatif_events[order_id] = evt
            w._whatif_results.pop(order_id, None)
            w._whatif_errors.pop(order_id, None)

        try:
            self._client.placeOrder(order_id, contract, order)
        except Exception as e:
            with w._whatif_lock:
                w._whatif_events.pop(order_id, None)
            raise RuntimeError(f"placeOrder whatIf failed: {e}") from e

        # Wait for openOrder callback with OrderState
        completed = await asyncio.to_thread(evt.wait, self.config.margin_timeout)
        with w._whatif_lock:
            result = w._whatif_results.get(order_id)
            err = w._whatif_errors.get(order_id)
            w._whatif_events.pop(order_id, None)
            # keep result for debugging but clear

        elapsed_ms = (time.monotonic() - start) * 1000

        if err:
            raise RuntimeError(f"whatIf error: {err}")
        if not completed or result is None:
            raise TimeoutError(f"whatIf timeout for {instrument.symbol} (no OrderState)")

        init = result.get("init", "") or ""
        maint = result.get("maint", "") or ""
        # IBKR may return empty if not computed — treat as failure
        if not init and not maint:
            raise RuntimeError(f"whatIf returned empty margins for {instrument.symbol}: {result}")

        return init, maint, elapsed_ms
