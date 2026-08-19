"""Unrealized P&L from signed quantities and an external mark. Does not invent prices."""

import logging
from decimal import Decimal

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
    leg_a_mark: Decimal,
    leg_b_signed: Decimal | None,
    leg_b_entry: Decimal | None,
    leg_b_mark: Decimal | None,
) -> Decimal:
    total = unrealized_leg(leg_a_signed, leg_a_entry, leg_a_mark)
    if (
        leg_b_signed is not None
        and leg_b_entry is not None
        and leg_b_mark is not None
    ):
        total += unrealized_leg(leg_b_signed, leg_b_entry, leg_b_mark)
    return total


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
        self._marks: dict[tuple[int, str, str], Decimal] = {}
        self._quotes: dict[tuple[int, str, str], dict[str, Decimal]] = {}
        self._legs: dict[tuple[int, str], dict[str, tuple[Decimal, Decimal]]] = {}
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
        for req_id, (acct, trade, symbol) in list(self._by_req.items()):
            if acct == account_id and trade == trade_id:
                self._quotes.pop((acct, trade, symbol), None)
                self._marks.pop((acct, trade, symbol), None)
        cancel = getattr(self._client, "cancelMktData", None)
        if not callable(cancel):
            logger.info(
                "LivePnl unwatch: account_id=%s trade_id=%s (no cancelMktData)",
                account_id,
                trade_id,
            )
            return
        for req_id, (acct, trade, _sym) in list(self._by_req.items()):
            if acct == account_id and trade == trade_id:
                try:
                    cancel(req_id)
                except Exception:
                    logger.exception(
                        "LivePnl cancelMktData failed: req_id=%s account_id=%s trade_id=%s",
                        req_id,
                        account_id,
                        trade_id,
                    )
                self._by_req.pop(req_id, None)
        logger.info("LivePnl unwatch: account_id=%s trade_id=%s", account_id, trade_id)

    def on_tick_price(self, reqId: int, tickType: int, price: float) -> None:
        if price is None or price <= 0:
            return
        mapped = self._by_req.get(reqId)
        if mapped is None:
            return
        account_id, trade_id, symbol = mapped
        key = (account_id, trade_id, symbol)
        quote = self._quotes.setdefault(key, {})
        if tickType in _LAST_TICKS:
            quote["last"] = Decimal(str(price))
        elif tickType in _BID_TICKS:
            quote["bid"] = Decimal(str(price))
        elif tickType in _ASK_TICKS:
            quote["ask"] = Decimal(str(price))
        elif tickType in _CLOSE_TICKS:
            quote["close"] = Decimal(str(price))
        else:
            return
        mark = _effective_mark(quote)
        if mark is None:
            return
        self._marks[key] = mark
        self._recompute(account_id, trade_id)

    def on_tick_size(self, reqId: int, tickType: int, size: int) -> None:
        return

    def on_market_data_type(self, reqId: int, marketDataType: int) -> None:
        return

    def on_connection_closed(self) -> None:
        return

    def _request_ticks(self, account_id: int, trade_id: str, leg) -> None:
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

        if any(
            mapped[0] == account_id and mapped[1] == trade_id and mapped[2] == leg.symbol
            for mapped in self._by_req.values()
        ):
            return
        resolved = getattr(leg, "resolved", None)
        if resolved is None:
            try:
                resolved = resolve_leg(
                    symbol=leg.symbol,
                    instrument_type=leg.instrument_type,
                    market=leg.exchange,
                    currency=leg.currency,
                    con_id=leg.con_id,
                    catalog=self._catalog,
                )
            except InstrumentResolutionError as exc:
                # CFD without master metadata: do not subscribe as STK.
                logger.warning(
                    "LivePnl skip ticks: account_id=%s trade_id=%s symbol=%s reason=%s",
                    account_id,
                    trade_id,
                    leg.symbol,
                    exc,
                )
                return
        contract = ibkr_market_data_contract_from_resolved(resolved)
        con_id = getattr(contract, "conId", None) or None
        if (getattr(contract, "secType", "") or "").upper() == "CFD" and not con_id:
            logger.warning(
                "LivePnl reqMktData without conId: account_id=%s trade_id=%s symbol=%s "
                "secType=CFD — ticks may not arrive",
                account_id,
                trade_id,
                leg.symbol,
            )
        req_id = self._next_req
        self._next_req += 1
        self._by_req[req_id] = (account_id, trade_id, leg.symbol)
        try:
            req_type = getattr(self._client, "reqMarketDataType", None)
            if callable(req_type):
                req_type(3)
            req_mkt(req_id, contract, "", False, False, [])
        except Exception:
            self._by_req.pop(req_id, None)
            logger.exception(
                "LivePnl reqMktData failed: account_id=%s trade_id=%s symbol=%s",
                account_id,
                trade_id,
                leg.symbol,
            )
            return
        logger.info(
            "LivePnl reqMktData: req_id=%s account_id=%s trade_id=%s symbol=%s "
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
