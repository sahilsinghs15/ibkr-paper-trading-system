"""Unrealized P&L from signed quantities and an external mark. Does not invent prices."""

import logging
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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

    def __init__(self, session_factory: SessionFactory, client: object | None) -> None:
        self._session_factory = session_factory
        self._client = client
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
        if client is not None and hasattr(client, "register_market_data_listener"):
            client.register_market_data_listener(self)

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
        self._legs.pop((account_id, trade_id), None)
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
                        cancel(req_id)
                    except Exception:
                        logger.exception("LivePnl cancelMktData failed for req_id=%s", req_id)

        logger.info("LivePnl unwatch: account_id=%s trade_id=%s", account_id, trade_id)

    def on_error(self, reqId: int, errorCode: int, errorString: str) -> None:
        c_key = self._req_to_contract.get(reqId)
        if c_key is None:
            return
        import time

        self._cooldowns[c_key] = time.time() + 600.0  # 10 minute backoff
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

        if errorCode == 10089:
            health["status"] = "NO_LIVE_ENTITLEMENT_API_SUBSCRIPTION_REQUIRED"
            logger.warning(
                "LivePnl IBKR Market Data Warning (10089): symbol=%s reqId=%d message=%s — "
                "Account lacks live API entitlement for exchange",
                c_key[1], reqId, errorString
            )
        elif errorCode == 10167:
            health["status"] = "NO_LIVE_ENTITLEMENT_DELAYED"
            logger.warning(
                "LivePnl IBKR Market Data Notice (10167): symbol=%s reqId=%d message=%s",
                c_key[1], reqId, errorString
            )
        elif errorCode in (354, 300, 321):
            health["status"] = "NO_MARKET_DATA_ENTITLEMENT"
            logger.warning(
                "LivePnl IBKR Market Data Warning (%d): symbol=%s reqId=%d message=%s",
                errorCode, c_key[1], reqId, errorString
            )
        elif errorCode == 200:
            health["status"] = "UNRESOLVED_CONTRACT_SPEC"
            logger.warning(
                "LivePnl IBKR Contract Error (200): symbol=%s reqId=%d message=%s",
                c_key[1], reqId, errorString
            )

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
            health["last_tick_at"] = datetime.datetime.now(datetime.timezone.utc)
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
        return

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
                req_mkt(new_req_id, underlying, "221", False, False, [])

    def on_connection_closed(self) -> None:
        return

    def get_market_data_health(self) -> dict[str, Any]:
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
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
        if resolved is None:
            try:
                md_inst_type = "STK" if getattr(leg, "instrument_type", "").upper() in ("CFD", "STK", "ETF") else getattr(leg, "instrument_type", "STK")
                resolved = resolve_leg(
                    symbol=leg.symbol,
                    instrument_type=md_inst_type,
                    market=getattr(leg, "exchange", "SMART"),
                    currency=getattr(leg, "currency", "USD"),
                    con_id=getattr(leg, "con_id", None),
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
        import asyncio

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._persist(account_id, trade_id, pnl), loop
            )
        except Exception:
            logger.exception(
                "LivePnl persist schedule failed: account_id=%s trade_id=%s",
                account_id,
                trade_id,
            )
            return

        def _log_persist_error(done) -> None:
            try:
                done.result()
            except Exception:
                logger.exception(
                    "LivePnl persist failed: account_id=%s trade_id=%s",
                    account_id,
                    trade_id,
                )

        future.add_done_callback(_log_persist_error)

    async def _persist(self, account_id: int, trade_id: str, pnl: Decimal) -> None:
        async with self._session_factory() as session, session.begin():
            await PositionRepository(session).update_live_pnl(
                account_id=account_id, trade_id=trade_id, live_pnl=pnl
            )
