# 09-CONTRACT-MARKET-DATA.md — Contract Resolution & Market Data

---

## 1. AUTHORITATIVE CONTRACT RESOLUTION PATH

Contract resolution is owned by [`backend/app/instruments/resolver.py`](file:///home/dev3/Documents/ibkr-paper-trading-system/backend/app/instruments/resolver.py#L1).

### Resolution Pipeline
```
[Inbound Symbol + Instrument Type]
                │
                ▼
[Database Instrument Catalog Lookup]
  - Checks `instruments` table for (symbol, sec_type)
  - Retrieves `trade_conid` and `market_data_conid`
                │
                ▼
[Explicit SecType Mapping]
  - STK -> STK (unless PAPER_EXECUTE_STK_AS_CFD enabled)
  - ETF -> STK (cash ETF is STK at IBKR)
  - CFD -> CFD (requires catalog row or SMART/USD defaults)
  * Never falls back CFD -> STK!
                │
                ▼
[Build IBKR Contract Objects]
  ├─► Execution Contract: Contract(symbol, secType="CFD", conId=trade_conid)
  └─► Market Data Contract: Contract(symbol, secType="STK", conId=market_data_conid)
```

---

## 2. DUAL-CONTRACT CFD MECHANICS

At IBKR, CFD contracts frequently lack `LAST` trade ticks outside European trading hours, or tick only `BID`/`ASK` spreads. To provide continuous, accurate mark prices for PnL:
- **Execution Contract**: Targets `secType="CFD"` with the CFD's `trade_conid`. Used for `placeOrder()` and order execution.
- **Market Data Contract**: Targets `secType="STK"` with the underlying stock's `market_data_con_id`. Used for `reqMktData()` live marks.
- **Invariant**: The PnL calculation applies the underlying stock's mark price directly to the CFD position's signed quantity.

---

## 3. MARKET DATA SUBSCRIPTION LIFECYCLE

1. **Subscription (`LivePnlService.watch_open`)**:
   - Called immediately when an engine basket or manual order opens a position.
   - Paces subscription request through `GatewayRateLimiter.acquire(PRIORITY_MARKET_DATA)`.
   - Calls `client.reqMktData(req_id, contract, "", False, False, [])`.
2. **Tick Stream**:
   - `TWSClient.tickPrice()` dispatches incoming ticks to `LivePnlService.on_tick_price()`.
   - Tick types tracked: `LAST` (4, 68), `MARK` (37), `BID` (1, 66), `ASK` (2, 67), `CLOSE` (9, 75).
3. **Unsubscription (`LivePnlService.unwatch`)**:
   - Called when a position lot reaches `signed_qty == 0` (`status == "CLOSED"`).
   - If no other active position requires that contract, issues `client.cancelMktData(req_id)`.
