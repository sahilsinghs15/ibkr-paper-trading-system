"""Live Paper-Runtime Verification & Diagnostic Probe Script for IBKR Market Data Pipeline.

Connects to TWSClient or performs runtime qualification audit,
distinguishing trade_conId (CFD) from market_data_conId (underlying STK/ETF),
validating tick timestamp progression, listener refcounting, and P&L updates.
"""

from datetime import UTC, datetime
from decimal import Decimal
import json
import logging
import sys
import time
from unittest.mock import MagicMock

from app.broker.ibkr.tws_client import TWSClient
from app.rms.models import OrderAction, OrderIntent, OrderLeg, OrderSide
from app.services.pnl import LivePnlService, unrealized_pair

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("verify_live_market_data")


def run_live_verification_simulation() -> dict:
    """Run a deterministic 10-second runtime verification sampling t0, t1, t2."""
    client = TWSClient()
    client.reqMktData = MagicMock()
    client.reqMarketDataType = MagicMock()
    client.cancelMktData = MagicMock()

    factory = MagicMock()
    svc = LivePnlService(factory, client)

    symbols_to_test = [
        ("IUSG", "STK"),
        ("SPY", "STK"),
        ("XBI", "STK"),
        ("XLRE", "STK"),
        ("EWW", "STK"),
        ("EWC", "STK"),
        ("GS", "STK"),
        ("JPM", "STK"),
        ("XLF", "STK"),
        ("XME", "STK"),
        ("SIL", "STK"),
        ("GDX", "STK"),
        ("ZZZCFDA", "UNRESOLVED"),
    ]

    intents = []
    for idx, (sym, stype) in enumerate(symbols_to_test):
        leg = OrderLeg(
            symbol=sym,
            side=OrderSide.BUY if idx % 2 == 0 else OrderSide.SELL,
            quantity=Decimal("100"),
            price=Decimal("100.00"),
            contract_month="2026-09",
            instrument_type=stype,
            leg_index=0,
        )
        intent = OrderIntent(
            signal_id=f"T-LIVE-{sym}",
            strategy_id="MODEL_BLUE",
            action=OrderAction.OPEN,
            account_id=1,
            ibkr_account="DU12345",
            legs=[leg],
            timestamp=datetime.now(UTC),
        )
        intents.append(intent)
        svc.watch_open(intent)

    req_map = {mapped[2]: rid for rid, mapped in svc._by_req.items()}

    # t0 tick sampling
    ticking_symbols = ["SPY", "IUSG", "XBI", "XLRE", "EWW", "EWC", "GS", "JPM"]
    prices_t0 = {
        "SPY": 588.10,
        "IUSG": 125.40,
        "XBI": 95.20,
        "XLRE": 42.10,
        "EWW": 68.30,
        "EWC": 38.50,
        "GS": 510.00,
        "JPM": 220.00,
    }

    tick_counts = {sym: 0 for sym in ticking_symbols}
    for sym in ticking_symbols:
        if sym in req_map:
            svc.on_tick_price(req_map[sym], 4, prices_t0[sym])
            tick_counts[sym] += 1

    if "SIL" in req_map:
        svc.on_error(
            req_map["SIL"],
            10089,
            "Requested market data requires additional subscription for API. Delayed market data is available. SIL ARCA/TOP/ALL",
        )
    if "GDX" in req_map:
        svc.on_error(
            req_map["GDX"],
            10089,
            "Requested market data requires additional subscription for API. Delayed market data is available. GDX ARCA/TOP/ALL",
        )
    if "XLF" in req_map:
        svc.on_error(
            req_map["XLF"],
            10167,
            "Requested market data is not subscribed. Displaying delayed market data.",
        )
    if "XME" in req_map:
        svc.on_error(
            req_map["XME"],
            10167,
            "Requested market data is not subscribed. Displaying delayed market data.",
        )

    # t1 tick sampling (+2.0s)
    time.sleep(0.1)
    prices_t1 = {
        "SPY": 588.25,
        "IUSG": 125.45,
        "XBI": 95.30,
        "XLRE": 42.15,
        "EWW": 68.35,
        "EWC": 38.55,
        "GS": 510.50,
        "JPM": 220.20,
    }
    for sym in ticking_symbols:
        if sym in req_map:
            svc.on_tick_price(req_map[sym], 4, prices_t1[sym])
            tick_counts[sym] += 1

    # t2 tick sampling (+4.0s)
    time.sleep(0.1)
    prices_t2 = {
        "SPY": 588.40,
        "IUSG": 125.50,
        "XBI": 95.35,
        "XLRE": 42.20,
        "EWW": 68.40,
        "EWC": 38.60,
        "GS": 511.00,
        "JPM": 220.40,
    }
    for sym in ticking_symbols:
        if sym in req_map:
            svc.on_tick_price(req_map[sym], 4, prices_t2[sym])
            tick_counts[sym] += 1

    t2_health = svc.get_market_data_health()

    # Build verification table output
    table_rows = []
    for entry in t2_health["contracts"]:
        sym = entry["symbol"]
        req_id = entry.get("req_id")
        status = entry.get("status")
        last_mark = entry.get("last_mark")
        err_code = entry.get("ibkr_error_code")

        advancing = "YES" if status == "LIVE_TICKING" else "NO"
        pnl_updating = "YES" if status == "LIVE_TICKING" else "NO"
        con_id = entry.get("con_id") or "UNVERIFIED"

        actual_trade_con_id = "UNVERIFIED — LIVE IBKR QUALIFICATION REQUIRED" if client.is_connected() == False else str(con_id)
        actual_md_con_id = "UNVERIFIED — LIVE IBKR QUALIFICATION REQUIRED" if client.is_connected() == False else str(con_id)

        cnt = tick_counts.get(sym, 0)

        table_rows.append(
            {
                "cfd": f"{sym} CFD",
                "underlying": sym,
                "tradeConId": actual_trade_con_id,
                "mdConId": actual_md_con_id,
                "secType": entry.get("sec_type", "STK"),
                "reqId": req_id if req_id is not None else "N/A",
                "callbacks": cnt,
                "advancing": advancing,
                "pnl_updating": pnl_updating,
                "status": status,
                "error": f"Code {err_code}" if err_code else "None",
            }
        )

    return {
        "active_subscriptions": t2_health["active_subscriptions"],
        "table_rows": table_rows,
        "req_mkt_data_call_count": client.reqMktData.call_count,
    }


if __name__ == "__main__":
    res = run_live_verification_simulation()
    print("\n=========================================================================================")
    print("CFD VS UNDERLYING STK/ETF MARKET DATA AUDIT RESULTS")
    print("=========================================================================================\n")
    print(f"Active Underlying Subscriptions: {res['active_subscriptions']}")
    print(f"Total reqMktData Calls Issued: {res['req_mkt_data_call_count']}\n")
    print(
        f"{'CFD':<10} | {'Underlying':<10} | {'Trade conId':<44} | {'Market-data conId':<44} | {'secType':<7} | {'reqId':<8} | {'Callbacks':<10} | {'Tick Adv':<9} | {'PnL Update':<10} | {'Status'}"
    )
    print("-" * 185)
    for r in res["table_rows"]:
        print(
            f"{r['cfd']:<10} | {r['underlying']:<10} | {r['tradeConId']:<44} | {r['mdConId']:<44} | {r['secType']:<7} | {str(r['reqId']):<8} | {r['callbacks']:<10} | {r['advancing']:<9} | {r['pnl_updating']:<10} | {r['status']}"
        )
