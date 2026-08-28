"""Unrealized P&L from signed quantities and an external mark. Does not invent prices."""

import asyncio
import logging
import threading
import time
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.broker.ibkr.gateway_rate_limiter import PRIORITY_MARKET_DATA
from app.db.repositories.position_repository import PositionRepository
from app.rms.models import OrderAction, OrderIntent, OrderLeg, OrderSide

logger = logging.getLogger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]

# IBKR TickType — CFDs often tick BID/ASK (and delayed variants) without LAST.
_TICK_BID = 1
_TICK_ASK = 2
_TICK_LAST = 4
_TICK_CLOSE = 9
_TICK_MARK = 37
_TICK_DELAYED_BID = 66
_TICK_DELAYED_ASK = 67
_TICK_DELAYED_LAST = 68
_TICK_DELAYED_CLOSE = 75

_LAST_TICKS = {_TICK_LAST, _TICK_DELAYED_LAST, _TICK_MARK}
_BID_TICKS = {_TICK_BID, _TICK_DELAYED_BID}
_ASK_TICKS = {_TICK_ASK, _TICK_DELAYED_ASK}
_CLOSE_TICKS = {_TICK_CLOSE, _TICK_DELAYED_CLOSE}

# Minimum seconds between Postgres writes for the same (account_id, trade_id).
_PERSIST_MIN_INTERVAL_SEC = 1.0


def unrealized_leg(signed_qty: Decimal, entry: Decimal, mark: Decimal) -> Decimal:
    """Long: qty * (mark - entry). Short: negative qty * (mark - entry)."""
    return signed_qty * (mark - entry)


def unrealized_pair(
    *,
    leg_a_signed: Decimal,
    leg_a_entry: Decimal,
    leg_a_mark: Decimal | None,
    leg_b_signed: Decimal | None,
    leg_b_entry: Decimal | None,
    leg_b_mark: Decimal | None,
) -> Decimal | None:
    if leg_a_mark is None:
        return None
    if leg_b_signed is not None and leg_b_entry is not None:
        if leg_b_mark is None:
            return None
        return unrealized_leg(leg_a_signed, leg_a_entry, leg_a_mark) + unrealized_leg(
            leg_b_signed, leg_b_entry, leg_b_mark
        )
    return unrealized_leg(leg_a_signed, leg_a_entry, leg_a_mark)


def _effective_mark(quote: dict[str, Decimal]) -> Decimal | None:
    last = quote.get("last")
    if last is not None:
        return last
    bid = quote.get("bid")
    ask = quote.get("ask")
    if bid is not None and ask is not None:
        return (bid + ask) / Decimal(2)
    return quote.get("close")


class LivePnlService:
    """Subscribe to IBKR ticks for open pair positions and persist live_pnl.

    Requires TWSClient.reqMktData. Does not use entry price as a mark.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        client: object | None,
        *,
        rate_limiter: object | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._client = client
        self._rate_limiter = rate_limiter
        self._next_req = 50000
        self._by_req: dict[int, tuple[int, str, str]] = {}
        self._listeners_by_req: dict[int, set[tuple[int, str, str]]] = {}
        self._marks: dict[tuple[int, str, str], Decimal] = {}
        self._quotes: dict[tuple[int, str, str], dict[str, Decimal]] = {}
        self._legs: dict[tuple[int, str], dict[str, tuple[Decimal, Decimal]]] = {}
        self._contract_reqs: dict[tuple, int] = {}
        self._req_to_contract: dict[int, tuple] = {}
        self._contract_health: dict[tuple, dict[str, Any]] = {}
        self._cooldowns: dict[tuple, float] = {}
        self._loop = None
        self._catalog = None
        self._persist_lock = threading.Lock()
        self._pending_pnl: dict[tuple[int, str], Decimal] = {}
        self._last_persisted_pnl: dict[tuple[int, str], Decimal] = {}
        self._last_persist_at: dict[tuple[int, str], float] = {}
        self._persist_in_flight: set[tuple[int, str]] = set()
        self._persist_delayed: set[tuple[int, str]] = set()
        if client is not None and hasattr(client, "register_market_data_listener"):
            client.register_market_data_listener(self)

    def _schedule_paced_retry(self, callback) -> None:
        loop = getattr(self, "_loop", None)
        if loop is None or not loop.is_running():
            logger.debug("LivePnl paced retry skipped: no running event loop")
            return
        loop.call_later(0.05, callback)

    def _try_paced_call(self, request_type: str, callback, *, retry_callback) -> bool:
        if self._rate_limiter is None:
            callback()
            return True
        acquired = self._rate_limiter.try_acquire(PRIORITY_MARKET_DATA, request_type)
        if acquired is not None:
            callback()
            return True
        self._schedule_paced_retry(retry_callback)
        return False

    def _cancel_mkt_data_paced(self, req_id: int, cancel) -> None:
        def _send() -> None:
            cancel(req_id)

        def _retry() -> None:
            self._cancel_mkt_data_paced(req_id, cancel)

        self._try_paced_call("cancelMktData", _send, retry_callback=_retry)

    def watch_open(self, intent: OrderIntent) -> None:
        if intent.account_id is None or self._client is None:
            return
        import asyncio

        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "LivePnl watch_open: no running event loop; ticks will not persist"
            )
        key = (intent.account_id, intent.signal_id.split(":CLOSE")[0].split(":UNWIND:")[0])
        if key in self._legs:
            return
        legs: dict[str, tuple[Decimal, Decimal]] = {}
        for leg in intent.legs:
            signed = Decimal(str(leg.quantity))
            if leg.side == OrderSide.SELL:
                signed = -signed
            legs[leg.symbol] = (signed, leg.price)
            self._request_ticks(intent.account_id, key[1], leg)
        self._legs[key] = legs
        logger.info(
            "LivePnl watch_open: account_id=%s trade_id=%s symbols=%s",
            intent.account_id,
            key[1],
            list(legs.keys()),
        )

    def hydrate_from_position_rows(self, rows, *, catalog=None) -> None:
        """Re-subscribe market data for persisted OPEN positions. Does not execute orders."""
        if self._client is None:
            return
        self._catalog = catalog
        from app.db.repositories.position_repository import PositionRepository

        helper = PositionRepository.__new__(PositionRepository)
        for row in rows:
            trade = helper.to_open_trade(row)
            if not trade.legs:
                continue
            intent_legs = [
                OrderLeg(
                    symbol=leg.symbol,
                    side=leg.side,
                    quantity=float(leg.quantity),
                    price=leg.price,
                    contract_month="",
                    instrument_type=leg.instrument_type,
                    leg_index=index,
                )
                for index, leg in enumerate(trade.legs)
            ]
            self.watch_open(
                OrderIntent(
                    signal_id=row.trade_id,
                    strategy_id=row.strategy_id,
                    action=OrderAction.OPEN,
                    account_id=row.account_id,
                    legs=intent_legs,
                )
            )

    def unwatch(self, account_id: int, trade_id: str) -> None:
        trade_key = (account_id, trade_id)
        self._legs.pop(trade_key, None)
        with self._persist_lock:
            self._pending_pnl.pop(trade_key, None)
            self._last_persisted_pnl.pop(trade_key, None)
            self._last_persist_at.pop(trade_key, None)
            self._persist_in_flight.discard(trade_key)
            self._persist_delayed.discard(trade_key)
        cancel = getattr(self._client, "cancelMktData", None)

        for req_id, listeners in list(self._listeners_by_req.items()):
            to_remove = {l for l in listeners if l[0] == account_id and l[1] == trade_id}
            for l in to_remove:
                listeners.discard(l)
                self._quotes.pop(l, None)
                self._marks.pop(l, None)

            if not listeners:
                self._listeners_by_req.pop(req_id, None)
                self._by_req.pop(req_id, None)
                c_key = self._req_to_contract.pop(req_id, None)
                if c_key:
                    self._contract_reqs.pop(c_key, None)
                if callable(cancel):
                    try:
                        self._cancel_mkt_data_paced(req_id, cancel)
                    except Exception:
                        logger.exception("LivePnl cancelMktData failed for req_id=%s", req_id)

        logger.info("LivePnl unwatch: account_id=%s trade_id=%s", account_id, trade_id)

    def on_error(self, reqId: int, errorCode: int, errorString: str) -> None:
        c_key = self._req_to_contract.get(reqId)
        if c_key is None:
            return
        import time

        health = self._contract_health.setdefault(c_key, {
            "symbol": c_key[1],
            "sec_type": c_key[0],
            "status": "LIVE",
            "ibkr_error_code": None,
            "ibkr_error_string": None,
            "last_tick_at": None,
            "last_mark": None,
        })
        health["ibkr_error_code"] = errorCode
        health["ibkr_error_string"] = errorString

        if errorCode in (10089, 10167):
            # "Delayed market data is available" — fall back to delayed mode
            # instead of treating as fatal. IBKR will send delayed ticks
            # (15-min delay) which is far better than $0.00.
            health["status"] = "DELAYED_FALLBACK"
            logger.warning(
                "LivePnl IBKR Market Data Warning (%d): symbol=%s reqId=%d — "
                "Falling back to DELAYED market data. message=%s",
                errorCode, c_key[1], reqId, errorString
            )
            # Switch to delayed mode and IBKR will start sending delayed ticks
            # on the SAME reqId — no need to cancel and re-request
            req_type = getattr(self._client, "reqMarketDataType", None)
            if callable(req_type):
                req_type(3)  # 3 = delayed market data
                logger.info(
                    "LivePnl switched to DELAYED market data mode (type=3) for symbol=%s reqId=%d",
                    c_key[1], reqId
                )
            # Do NOT set cooldown — we want delayed ticks to flow
        elif errorCode in (354, 300, 321):
            self._cooldowns[c_key] = time.time() + 600.0  # 10 minute backoff
            health["status"] = "NO_MARKET_DATA_ENTITLEMENT"
            logger.warning(
                "LivePnl IBKR Market Data Warning (%d): symbol=%s reqId=%d message=%s",
                errorCode, c_key[1], reqId, errorString
            )
        elif errorCode == 1100:
            logger.warning("LivePnl IBKR Connectivity Lost (1100): message=%s", errorString)
        elif errorCode == 1101:
            logger.info("LivePnl IBKR Connectivity Restored - Data Lost (1101): Re-subscribing active streams...")
            self._cooldowns.clear()
            self._resubscribe_all_active()
        elif errorCode == 1102:
            logger.info("LivePnl IBKR Connectivity Restored - Data Maintained (1102): Stream active.")
        elif errorCode == 200:
            self._cooldowns[c_key] = time.time() + 600.0  # 10 minute backoff
            health["status"] = "UNRESOLVED_CONTRACT_SPEC"
            logger.warning(
                "LivePnl IBKR Contract Error (200): symbol=%s reqId=%d message=%s",
                c_key[1], reqId, errorString
            )

    def _resubscribe_all_active(self) -> None:
        """Re-subscribe all active contract requests after an 1101 connection recovery."""
        if not self._client or not hasattr(self._client, "reqMktData"):
            return
        for c_key, req_id in list(self._contract_reqs.items()):
            try:
                sec_type, symbol, exchange, ccy, con_id = c_key
                import ibapi.contract  # type: ignore

                contract = ibapi.contract.Contract()
                contract.symbol = symbol
                contract.secType = sec_type
                contract.exchange = exchange
                contract.currency = ccy
                if con_id:
                    contract.conId = con_id

                def _send(req=req_id, c=contract, sym=symbol) -> None:
                    req_mkt = getattr(self._client, "reqMktData", None)
                    if callable(req_mkt):
                        req_mkt(req, c, "", False, False, [])
                        logger.info(
                            "LivePnl re-subscribed market data for symbol=%s reqId=%d",
                            sym,
                            req,
                        )

                def _retry(req=req_id, c=contract, sym=symbol) -> None:
                    self._resubscribe_one(req, c, sym)

                self._try_paced_call("reqMktData", _send, retry_callback=_retry)
            except Exception:
                logger.exception(
                    "Failed to re-subscribe market data for symbol=%s reqId=%d",
                    c_key[1],
                    req_id,
                )

    def _resubscribe_one(self, req_id: int, contract, symbol: str) -> None:
        def _send() -> None:
            req_mkt = getattr(self._client, "reqMktData", None)
            if callable(req_mkt):
                req_mkt(req_id, contract, "", False, False, [])
                logger.info(
                    "LivePnl re-subscribed market data for symbol=%s reqId=%d",
                    symbol,
                    req_id,
                )

        def _retry() -> None:
            self._resubscribe_one(req_id, contract, symbol)

        self._try_paced_call("reqMktData", _send, retry_callback=_retry)

    def on_tick_price(self, reqId: int, tickType: int, price: float) -> None:
        if price is None or price <= 0:
            return
        listeners = self._listeners_by_req.get(reqId)
        if not listeners:
            mapped = self._by_req.get(reqId)
            listeners = {mapped} if mapped else set()
        if not listeners:
            return

        dec_price = Decimal(str(price))
        updated_trades = set()
        for (account_id, trade_id, symbol) in list(listeners):
            key = (account_id, trade_id, symbol)
            quote = self._quotes.setdefault(key, {})
            if tickType in _LAST_TICKS:
                quote["last"] = dec_price
            elif tickType in _BID_TICKS:
                quote["bid"] = dec_price
            elif tickType in _ASK_TICKS:
                quote["ask"] = dec_price
            elif tickType in _CLOSE_TICKS:
                quote["close"] = dec_price
            else:
                continue
            mark = _effective_mark(quote)
            if mark is not None:
                self._marks[key] = mark
                updated_trades.add((account_id, trade_id))

        c_key = self._req_to_contract.get(reqId)
        if c_key:
            import datetime
            health = self._contract_health.setdefault(c_key, {
                "symbol": c_key[1],
                "sec_type": c_key[0],
                "status": "LIVE",
                "ibkr_error_code": None,
                "ibkr_error_string": None,
                "last_tick_at": None,
                "last_mark": None,
            })
            health["status"] = "LIVE"
            health["last_tick_at"] = datetime.datetime.now(datetime.UTC)
            if listeners:
                any_key = next(iter(listeners))
                mark = self._marks.get(any_key)
                if mark is not None:
                    health["last_mark"] = str(mark)

        for account_id, trade_id in updated_trades:
            self._recompute(account_id, trade_id)

    def on_tick_size(self, reqId: int, tickType: int, size: int) -> None:
        return

    def on_market_data_type(self, reqId: int, marketDataType: int) -> None:
        # 1=live, 2=frozen, 3=delayed, 4=delayed-frozen
        _type_names = {1: "LIVE", 2: "FROZEN", 3: "DELAYED", 4: "DELAYED_FROZEN"}
        c_key = self._req_to_contract.get(reqId)
        symbol = c_key[1] if c_key else "?"
        logger.info(
            "LivePnl marketDataType callback: reqId=%d symbol=%s type=%d (%s)",
            reqId, symbol, marketDataType, _type_names.get(marketDataType, "UNKNOWN")
        )

    def on_reroute_mkt_data(self, reqId: int, conId: int, exchange: str) -> None:
        c_key = self._req_to_contract.get(reqId)
        if not c_key:
            return
        symbol = c_key[1]
        logger.info(
            "LivePnl on_reroute_mkt_data: reqId=%d symbol=%s underlying_conId=%d exchange=%s",
            reqId, symbol, conId, exchange
        )
        from ibapi.contract import Contract
        underlying = Contract()
        underlying.conId = conId
        underlying.symbol = symbol
        underlying.secType = "STK"
        underlying.exchange = exchange or "SMART"
        underlying.currency = "USD"

        new_c_key = ("STK", symbol, underlying.exchange, "USD", conId)
        if new_c_key not in self._contract_reqs:
            new_req_id = self._next_req
            self._next_req += 1
            listeners = self._listeners_by_req.get(reqId, set())
            mapped = self._by_req.get(reqId)
            if mapped:
                self._by_req[new_req_id] = mapped
            self._contract_reqs[new_c_key] = new_req_id
            self._req_to_contract[new_req_id] = new_c_key
            self._listeners_by_req[new_req_id] = set(listeners)

            req_mkt = getattr(self._client, "reqMktData", None)
            if callable(req_mkt):
                def _send() -> None:
                    req_mkt(new_req_id, underlying, "221", False, False, [])

                def _retry() -> None:
                    self._issue_reroute_mkt_data(new_req_id, underlying)

                self._try_paced_call("reqMktData", _send, retry_callback=_retry)

    def _issue_reroute_mkt_data(self, req_id: int, contract) -> None:
        req_mkt = getattr(self._client, "reqMktData", None)
        if not callable(req_mkt):
            return

        def _send() -> None:
            req_mkt(req_id, contract, "221", False, False, [])

        def _retry() -> None:
            self._issue_reroute_mkt_data(req_id, contract)

        self._try_paced_call("reqMktData", _send, retry_callback=_retry)

    def on_connection_closed(self) -> None:
        return

    def get_market_data_health(self) -> dict[str, Any]:
        import datetime
        now = datetime.datetime.now(datetime.UTC)
        STALE_THRESHOLD_SEC = 15.0
        contracts_out = []
        for c_key, health in self._contract_health.items():
            last_ts = health.get("last_tick_at")
            tick_age_sec = (now - last_ts).total_seconds() if last_ts else None
            req_id = self._contract_reqs.get(c_key)
            listeners = self._listeners_by_req.get(req_id, set()) if req_id is not None else set()

            raw_status = health.get("status", "NO_MARK")
            if raw_status in ("LIVE", "LIVE_TICKING"):
                if tick_age_sec is not None and tick_age_sec > STALE_THRESHOLD_SEC:
                    computed_status = "STALE_TICK"
                else:
                    computed_status = raw_status
            else:
                computed_status = raw_status

            contracts_out.append({
                "symbol": c_key[1],
                "sec_type": c_key[0],
                "exchange": c_key[2],
                "currency": c_key[3] if len(c_key) > 3 else "USD",
                "con_id": c_key[4] if len(c_key) > 4 else None,
                "req_id": req_id,
                "status": computed_status,
                "listener_count": len(listeners),
                "last_tick_timestamp": last_ts.isoformat() if last_ts else None,
                "last_tick_age_sec": round(tick_age_sec, 2) if tick_age_sec is not None else None,
                "last_mark": health.get("last_mark"),
                "ibkr_error_code": health.get("ibkr_error_code"),
                "ibkr_error_string": health.get("ibkr_error_string"),
            })
        return {
            "active_subscriptions": len(self._contract_reqs),
            "contracts": contracts_out,
        }

    def _request_ticks(self, account_id: int, trade_id: str, leg) -> None:
        sym_clean = (leg.symbol or "").strip().upper()
        if sym_clean.startswith("ZZZ") or "ZZZCFD" in sym_clean:
            logger.warning(
                "LivePnl skip ticks: synthetic/test symbol=%s account_id=%s trade_id=%s",
                leg.symbol,
                account_id,
                trade_id,
            )
            c_key = ("UNRESOLVED", leg.symbol, "SMART", "USD", 0)
            self._contract_health[c_key] = {
                "symbol": leg.symbol,
                "sec_type": "UNRESOLVED",
                "status": "UNRESOLVED_CONTRACT_SPEC",
                "ibkr_error_code": 200,
                "ibkr_error_string": "Synthetic test symbol — not sent to IBKR",
                "last_tick_at": None,
                "last_mark": None,
            }
            return

        req_mkt = getattr(self._client, "reqMktData", None)
        if not callable(req_mkt):
            logger.warning(
                "LivePnl skip ticks: account_id=%s trade_id=%s symbol=%s reason=no reqMktData",
                account_id,
                trade_id,
                leg.symbol,
            )
            return
        from app.instruments.models import InstrumentResolutionError
        from app.instruments.resolver import (
            ibkr_market_data_contract_from_resolved,
            resolve_leg,
        )

        resolved = getattr(leg, "resolved", None)
        if resolved is None or getattr(resolved, "sec_type", "").upper() == "CFD":
            try:
                md_inst_type = "STK" if getattr(leg, "instrument_type", "").upper() in ("CFD", "STK", "ETF") else getattr(leg, "instrument_type", "STK")
                resolved = resolve_leg(
                    symbol=leg.symbol,
                    instrument_type=md_inst_type,
                    market=getattr(leg, "exchange", "SMART"),
                    currency=getattr(leg, "currency", "USD"),
                    con_id=getattr(resolved, "market_data_con_id", None),
                    catalog=self._catalog,
                    apply_demo_override=False,  # Market data uses STK for equities/ETFs
                )
            except InstrumentResolutionError as exc:
                logger.warning(
                    "LivePnl skip ticks: account_id=%s trade_id=%s symbol=%s reason=%s",
                    account_id,
                    trade_id,
                    leg.symbol,
                    exc,
                )
                c_key = ("UNRESOLVED", leg.symbol, "SMART", "USD", 0)
                self._contract_health[c_key] = {
                    "symbol": leg.symbol,
                    "sec_type": "UNRESOLVED",
                    "status": "UNRESOLVED_CONTRACT_SPEC",
                    "ibkr_error_code": 200,
                    "ibkr_error_string": str(exc),
                    "last_tick_at": None,
                    "last_mark": None,
                }
                return
        contract = ibkr_market_data_contract_from_resolved(resolved)
        if getattr(contract, "secType", "").upper() == "CFD":
            contract.secType = "STK"
            contract.conId = getattr(resolved, "market_data_con_id", None) or 0

        # Live IBKR Contract Qualification if connected
        is_conn = getattr(self._client, "is_connected", None)
        if callable(is_conn) and is_conn():
            req_details = getattr(self._client, "request_contract_details", None)
            if callable(req_details):
                try:
                    details = req_details(contract, timeout=3.0)
                    if details:
                        qualified_c = details[0].contract
                        if getattr(qualified_c, "conId", 0):
                            contract.conId = qualified_c.conId
                        if getattr(qualified_c, "primaryExchange", None):
                            contract.primaryExchange = qualified_c.primaryExchange
                    else:
                        logger.warning(
                            "LivePnl IBKR Contract Qualification: 0 details returned for symbol=%s",
                            leg.symbol,
                        )
                except Exception:
                    logger.exception(
                        "LivePnl IBKR Contract Qualification exception for symbol=%s",
                        leg.symbol,
                    )

        c_key = (
            getattr(contract, "secType", "STK"),
            getattr(contract, "symbol", leg.symbol),
            getattr(contract, "exchange", "SMART"),
            getattr(contract, "currency", "USD"),
            getattr(contract, "conId", 0) or 0,
        )

        import time
        if time.time() < self._cooldowns.get(c_key, 0):
            logger.info(
                "LivePnl skip ticks (cooldown active): symbol=%s account_id=%s trade_id=%s",
                leg.symbol, account_id, trade_id
            )
            return

        # Check if contract is already subscribed
        if c_key in self._contract_reqs:
            existing_req_id = self._contract_reqs[c_key]
            self._by_req[existing_req_id] = (account_id, trade_id, leg.symbol)
            self._listeners_by_req.setdefault(existing_req_id, set()).add((account_id, trade_id, leg.symbol))
            logger.info(
                "LivePnl reuse subscription: req_id=%s account_id=%s trade_id=%s symbol=%s",
                existing_req_id,
                account_id,
                trade_id,
                leg.symbol,
            )
            return

        req_id = self._next_req
        self._next_req += 1
        self._by_req[req_id] = (account_id, trade_id, leg.symbol)
        self._contract_reqs[c_key] = req_id
        self._req_to_contract[req_id] = c_key
        self._listeners_by_req.setdefault(req_id, set()).add((account_id, trade_id, leg.symbol))

        def _send() -> None:
            try:
                req_type = getattr(self._client, "reqMarketDataType", None)
                if callable(req_type):
                    req_type(1)  # REALTIME live market data mode
                req_mkt(req_id, contract, "221", False, False, [])
            except Exception:
                self._by_req.pop(req_id, None)
                self._contract_reqs.pop(c_key, None)
                self._req_to_contract.pop(req_id, None)
                self._listeners_by_req.pop(req_id, None)
                logger.exception(
                    "LivePnl reqMktData failed: account_id=%s trade_id=%s symbol=%s",
                    account_id,
                    trade_id,
                    leg.symbol,
                )
                return
            logger.info(
                "LivePnl reqMktData REALTIME: req_id=%s account_id=%s trade_id=%s symbol=%s "
                "secType=%s conId=%s",
                req_id,
                account_id,
                trade_id,
                getattr(contract, "symbol", None),
                getattr(contract, "secType", None),
                getattr(contract, "conId", None) or None,
            )

        def _retry() -> None:
            self._issue_request_ticks(account_id, trade_id, leg, req_id, contract, c_key, req_mkt)

        if not self._try_paced_call("reqMktData", _send, retry_callback=_retry):
            logger.debug(
                "LivePnl reqMktData deferred: account_id=%s trade_id=%s symbol=%s",
                account_id,
                trade_id,
                leg.symbol,
            )

    def _issue_request_ticks(
        self,
        account_id: int,
        trade_id: str,
        leg,
        req_id: int,
        contract,
        c_key: tuple,
        req_mkt,
    ) -> None:
        def _send() -> None:
            try:
                req_type = getattr(self._client, "reqMarketDataType", None)
                if callable(req_type):
                    req_type(1)
                req_mkt(req_id, contract, "221", False, False, [])
            except Exception:
                self._by_req.pop(req_id, None)
                self._contract_reqs.pop(c_key, None)
                self._req_to_contract.pop(req_id, None)
                self._listeners_by_req.pop(req_id, None)
                logger.exception(
                    "LivePnl reqMktData failed: account_id=%s trade_id=%s symbol=%s",
                    account_id,
                    trade_id,
                    leg.symbol,
                )
                return
            logger.info(
                "LivePnl reqMktData REALTIME: req_id=%s account_id=%s trade_id=%s symbol=%s",
                req_id,
                account_id,
                trade_id,
                getattr(contract, "symbol", None),
            )

        def _retry() -> None:
            self._issue_request_ticks(
                account_id, trade_id, leg, req_id, contract, c_key, req_mkt
            )

        self._try_paced_call("reqMktData", _send, retry_callback=_retry)

    def _recompute(self, account_id: int, trade_id: str) -> None:
        legs = self._legs.get((account_id, trade_id))
        if not legs:
            return
        symbols = list(legs.keys())
        marks = []
        for symbol in symbols:
            mark = self._marks.get((account_id, trade_id, symbol))
            if mark is None:
                return
            marks.append(mark)
        signed_a, entry_a = legs[symbols[0]]
        pnl = unrealized_leg(signed_a, entry_a, marks[0])
        if len(symbols) > 1:
            signed_b, entry_b = legs[symbols[1]]
            pnl += unrealized_leg(signed_b, entry_b, marks[1])
        loop = getattr(self, "_loop", None)
        if loop is None or not loop.is_running():
            return
        trade_key = (account_id, trade_id)
        with self._persist_lock:
            self._pending_pnl[trade_key] = pnl
        try:
            asyncio.run_coroutine_threadsafe(
                self._schedule_persist(account_id, trade_id), loop
            )
        except Exception:
            logger.exception(
                "LivePnl persist schedule failed: account_id=%s trade_id=%s",
                account_id,
                trade_id,
            )

    async def _schedule_persist(self, account_id: int, trade_id: str) -> None:
        """Coalesce pending pnl into at most one in-flight persist per trade."""
        trade_key = (account_id, trade_id)
        pnl_to_write: Decimal | None = None

        with self._persist_lock:
            if trade_key not in self._legs:
                return
            pending = self._pending_pnl.get(trade_key)
            if pending is None:
                return
            if pending == self._last_persisted_pnl.get(trade_key):
                self._pending_pnl.pop(trade_key, None)
                return
            if trade_key in self._persist_in_flight:
                return
            now = time.monotonic()
            last_at = self._last_persist_at.get(trade_key)
            if last_at is not None and (now - last_at) < _PERSIST_MIN_INTERVAL_SEC:
                wait = _PERSIST_MIN_INTERVAL_SEC - (now - last_at)
                if trade_key not in self._persist_delayed:
                    self._persist_delayed.add(trade_key)
                    loop = asyncio.get_running_loop()
                    loop.call_later(
                        wait,
                        lambda ak=account_id, tid=trade_id: asyncio.create_task(
                            self._schedule_persist(ak, tid)
                        ),
                    )
                return
            pnl_to_write = pending
            self._persist_in_flight.add(trade_key)

        if pnl_to_write is None:
            return

        try:
            await self._persist(account_id, trade_id, pnl_to_write)
        except Exception:
            logger.exception(
                "LivePnl persist failed: account_id=%s trade_id=%s",
                account_id,
                trade_id,
            )
        else:
            with self._persist_lock:
                self._last_persisted_pnl[trade_key] = pnl_to_write
                self._last_persist_at[trade_key] = time.monotonic()
                if self._pending_pnl.get(trade_key) == pnl_to_write:
                    self._pending_pnl.pop(trade_key, None)
        finally:
            with self._persist_lock:
                self._persist_in_flight.discard(trade_key)
                self._persist_delayed.discard(trade_key)

        await self._schedule_persist(account_id, trade_id)

    async def _persist(self, account_id: int, trade_id: str, pnl: Decimal) -> None:
        async with self._session_factory() as session, session.begin():
            await PositionRepository(session).update_live_pnl(
                account_id=account_id, trade_id=trade_id, live_pnl=pnl
            )
