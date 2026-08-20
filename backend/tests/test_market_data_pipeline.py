"""Regression tests for IBKR live market data tick subscription pipeline, contract deduplication, and error tracking.

Tests cover:
1. REALTIME mode (reqMarketDataType(1)) is called on tick requests.
2. Contract deduplication: duplicate positions for the same symbol reuse req_id.
3. TWS Error 10167 tracks contract health as NO_LIVE_ENTITLEMENT_DELAYED.
4. TWS Error 354 tracks contract health as NO_MARKET_DATA_ENTITLEMENT.
5. Error on one symbol (e.g. GS) does not block tick processing for other symbols (GDX/SIL).
6. get_market_data_health() outputs contract health snapshot with tick ages.
7. Unwatching a position refcounts subscriptions and only cancels when subscriber count is zero.
8. TWSClient forwards error callbacks to market data listeners.
9. Synthetic test symbols (ZZZCFDA/ZZZCFDB) marked UNRESOLVED_CONTRACT_SPEC without calling reqMktData.
10. TWS Error 10089 tracks health as NO_LIVE_ENTITLEMENT_API_SUBSCRIPTION_REQUIRED.
11. unrealized_pair requires marks for both legs when Leg B is defined.
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.broker.ibkr.tws_client import TWSClient
from app.rms.models import OrderAction, OrderIntent, OrderLeg, OrderSide
from app.services.pnl import LivePnlService, unrealized_pair


def _make_intent(
    signal_id: str = "T-MD-1",
    account_id: int = 1,
    symbols: list[str] | None = None,
) -> OrderIntent:
    if symbols is None:
        symbols = ["GS", "JPM"]
    legs = [
        OrderLeg(
            symbol=sym,
            side=OrderSide.BUY if idx == 0 else OrderSide.SELL,
            quantity=Decimal("100"),
            price=Decimal("150.00"),
            contract_month="2026-09",
            instrument_type="STK",
            leg_index=idx,
        )
        for idx, sym in enumerate(symbols)
    ]
    return OrderIntent(
        signal_id=signal_id,
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        account_id=account_id,
        ibkr_account="DU12345",
        legs=legs,
        timestamp=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Test 1 — REALTIME mode (reqMarketDataType(1)) is called on tick requests
# ---------------------------------------------------------------------------
def test_1_realtime_mode_requested_on_ticks() -> None:
    client = MagicMock()
    client.reqMktData = MagicMock()
    client.reqMarketDataType = MagicMock()

    factory = MagicMock()
    svc = LivePnlService(factory, client)
    intent = _make_intent("T-REALTIME-1", symbols=["GS"])

    svc.watch_open(intent)

    client.reqMarketDataType.assert_called_with(1)
    client.reqMktData.assert_called_once()


# ---------------------------------------------------------------------------
# Test 2 — Contract deduplication: duplicate positions reuse single reqId
# ---------------------------------------------------------------------------
def test_2_contract_deduplication_reuses_req_id() -> None:
    client = MagicMock()
    client.reqMktData = MagicMock()
    client.reqMarketDataType = MagicMock()

    factory = MagicMock()
    svc = LivePnlService(factory, client)

    # Position 1 using GS and JPM
    intent1 = _make_intent("T-DEDUP-1", account_id=1, symbols=["GS", "JPM"])
    svc.watch_open(intent1)
    initial_call_count = client.reqMktData.call_count
    assert initial_call_count == 2

    # Position 2 using GS and XLF (GS is a duplicate contract)
    intent2 = _make_intent("T-DEDUP-2", account_id=2, symbols=["GS", "XLF"])
    svc.watch_open(intent2)

    # Total reqMktData calls should be 3 (GS reused, XLF new), not 4
    assert client.reqMktData.call_count == 3
    assert len(svc._contract_reqs) == 3


# ---------------------------------------------------------------------------
# Test 3 — TWS Error 10167 tracks health as NO_LIVE_ENTITLEMENT_DELAYED
# ---------------------------------------------------------------------------
def test_3_tws_error_10167_tracks_health_status() -> None:
    client = MagicMock()
    client.reqMktData = MagicMock()
    client.reqMarketDataType = MagicMock()

    factory = MagicMock()
    svc = LivePnlService(factory, client)
    intent = _make_intent("T-ERR-10167", symbols=["GS"])

    svc.watch_open(intent)
    req_id = list(svc._by_req.keys())[0]

    # Deliver TWS Error 10167
    svc.on_error(req_id, 10167, "Requested market data is not subscribed. Displaying delayed market data.")

    health = svc.get_market_data_health()
    gs_health = next(c for c in health["contracts"] if c["symbol"] == "GS")

    assert gs_health["status"] == "NO_LIVE_ENTITLEMENT_DELAYED"
    assert gs_health["ibkr_error_code"] == 10167


# ---------------------------------------------------------------------------
# Test 4 — TWS Error 354 tracks health as NO_MARKET_DATA_ENTITLEMENT
# ---------------------------------------------------------------------------
def test_4_tws_error_354_tracks_health_status() -> None:
    client = MagicMock()
    client.reqMktData = MagicMock()
    client.reqMarketDataType = MagicMock()

    factory = MagicMock()
    svc = LivePnlService(factory, client)
    intent = _make_intent("T-ERR-354", symbols=["XME"])

    svc.watch_open(intent)
    req_id = list(svc._by_req.keys())[0]

    # Deliver TWS Error 354
    svc.on_error(req_id, 354, "Requested market data is not subscribed.")

    health = svc.get_market_data_health()
    xme_health = next(c for c in health["contracts"] if c["symbol"] == "XME")

    assert xme_health["status"] == "NO_MARKET_DATA_ENTITLEMENT"
    assert xme_health["ibkr_error_code"] == 354


# ---------------------------------------------------------------------------
# Test 5 — Error on one symbol (GS) does not block ticks for other symbols (GDX)
# ---------------------------------------------------------------------------
def test_5_symbol_error_isolation() -> None:
    client = MagicMock()
    client.reqMktData = MagicMock()
    client.reqMarketDataType = MagicMock()

    factory = MagicMock()
    svc = LivePnlService(factory, client)
    intent = _make_intent("T-ISO-1", symbols=["GS", "GDX"])

    svc.watch_open(intent)
    gs_req = next(rid for rid, mapped in svc._by_req.items() if mapped[2] == "GS")
    gdx_req = next(rid for rid, mapped in svc._by_req.items() if mapped[2] == "GDX")

    # GS receives error 10167
    svc.on_error(gs_req, 10167, "Requested market data is not subscribed.")
    # GDX receives live tick price
    svc.on_tick_price(gdx_req, 4, 35.50)

    health = svc.get_market_data_health()
    gs_health = next(c for c in health["contracts"] if c["symbol"] == "GS")
    gdx_health = next(c for c in health["contracts"] if c["symbol"] == "GDX")

    assert gs_health["status"] == "NO_LIVE_ENTITLEMENT_DELAYED"
    assert gdx_health["status"] == "LIVE"
    assert gdx_health["last_mark"] in ("35.5", "35.50")


# ---------------------------------------------------------------------------
# Test 6 — Unwatching position refcounts subscriptions properly
# ---------------------------------------------------------------------------
def test_6_unwatch_refcounts_and_cancels_only_when_empty() -> None:
    client = MagicMock()
    client.reqMktData = MagicMock()
    client.cancelMktData = MagicMock()

    factory = MagicMock()
    svc = LivePnlService(factory, client)

    # Position 1 & Position 2 both watch GS
    intent1 = _make_intent("T-REF-1", account_id=1, symbols=["GS"])
    intent2 = _make_intent("T-REF-2", account_id=2, symbols=["GS"])
    svc.watch_open(intent1)
    svc.watch_open(intent2)

    # Unwatch Position 1: GS is still watched by Position 2, so cancelMktData should NOT be called yet
    svc.unwatch(1, "T-REF-1")
    client.cancelMktData.assert_not_called()

    # Unwatch Position 2: Position count reaches 0, cancelMktData SHOULD be called
    svc.unwatch(2, "T-REF-2")
    client.cancelMktData.assert_called_once()


# ---------------------------------------------------------------------------
# Test 7 — TWSClient forwards error callbacks to market data listeners
# ---------------------------------------------------------------------------
def test_7_tws_client_forwards_errors_to_market_data_listeners() -> None:
    tws = TWSClient()
    listener = MagicMock()
    tws.register_market_data_listener(listener)

    tws.error(50001, 10167, "Requested market data is not subscribed.")

    listener.on_error.assert_called_once_with(50001, 10167, "Requested market data is not subscribed.")


# ---------------------------------------------------------------------------
# Test 8 — Synthetic test symbols (ZZZCFDA) filtered out before reqMktData
# ---------------------------------------------------------------------------
def test_8_synthetic_symbols_filtered_out() -> None:
    client = MagicMock()
    client.reqMktData = MagicMock()

    factory = MagicMock()
    svc = LivePnlService(factory, client)
    intent = _make_intent("T-SYNTH-1", symbols=["ZZZCFDA", "ZZZCFDB"])

    svc.watch_open(intent)

    # reqMktData should NOT be called for synthetic symbols
    client.reqMktData.assert_not_called()

    health = svc.get_market_data_health()
    zzz_health = next(c for c in health["contracts"] if c["symbol"] == "ZZZCFDA")
    assert zzz_health["status"] == "UNRESOLVED_CONTRACT_SPEC"


# ---------------------------------------------------------------------------
# Test 9 — TWS Error 10089 tracks health as NO_LIVE_ENTITLEMENT_API_SUBSCRIPTION_REQUIRED
# ---------------------------------------------------------------------------
def test_9_tws_error_10089_tracks_health_status() -> None:
    client = MagicMock()
    client.reqMktData = MagicMock()
    client.reqMarketDataType = MagicMock()

    factory = MagicMock()
    svc = LivePnlService(factory, client)
    intent = _make_intent("T-ERR-10089", symbols=["SIL"])

    svc.watch_open(intent)
    req_id = list(svc._by_req.keys())[0]

    # Deliver TWS Error 10089
    svc.on_error(
        req_id,
        10089,
        "Requested market data requires additional subscription for API. Delayed market data is available. SIL ARCA/TOP/ALL",
    )

    health = svc.get_market_data_health()
    sil_health = next(c for c in health["contracts"] if c["symbol"] == "SIL")

    assert sil_health["status"] == "NO_LIVE_ENTITLEMENT_API_SUBSCRIPTION_REQUIRED"
    assert sil_health["ibkr_error_code"] == 10089


# ---------------------------------------------------------------------------
# Test 10 — Two-leg spread P&L requires marks for both legs
# ---------------------------------------------------------------------------
def test_10_unrealized_pair_requires_both_leg_marks() -> None:
    # Leg A has mark 100, Leg B mark is None -> returns None
    pnl = unrealized_pair(
        leg_a_signed=Decimal("10"),
        leg_a_entry=Decimal("90"),
        leg_a_mark=Decimal("100"),
        leg_b_signed=Decimal("-10"),
        leg_b_entry=Decimal("50"),
        leg_b_mark=None,
    )
    assert pnl is None

    # Both legs have marks -> returns calculated spread P&L
    pnl_both = unrealized_pair(
        leg_a_signed=Decimal("10"),
        leg_a_entry=Decimal("90"),
        leg_a_mark=Decimal("100"),
        leg_b_signed=Decimal("-10"),
        leg_b_entry=Decimal("50"),
        leg_b_mark=Decimal("45"),
    )
    # Leg A: 10 * (100 - 90) = 100
    # Leg B: -10 * (45 - 50) = 50
    # Total: 150
    assert pnl_both == Decimal("150")
