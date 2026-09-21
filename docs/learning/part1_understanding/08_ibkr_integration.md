# Chapter 8 — IBKR Integration

## Current Implementation

### Responsibility

Own the single broker connection: socket lifecycle, outbound pacing, contract
resolution, market data subscriptions, and delivery of callbacks into the
system.

### Major components

| Component | File | Role |
|---|---|---|
| `TWSClient` | `backend/app/broker/ibkr/tws_client.py` | The only socket; reader thread |
| `GatewayRateLimiter` | `backend/app/broker/ibkr/gateway_rate_limiter.py` | All outbound pacing |
| `IBKRExecutionAdapter` | `backend/app/oms/ibkr_adapter.py` | Order submission, callback routing |
| `InstrumentResolver` | `backend/app/instruments/resolver.py` | CFD↔underlying, size increments |

### Hard rules

From `AGENTS.md` §6 and §7, all DO-NOT-BYPASS:

- Exactly one `TWSClient` socket. Never a second client or raw socket.
- Every outbound call passes through `GatewayRateLimiter.acquire()`.
- Order ids come from `allocate_next_order_id()` — never hardcoded, never
  defaulted to 1.
- CFD orders set `secType="CFD"`, `outsideRth=False`, `eTradeOnly=False`,
  `firmQuoteOnly=False`.
- Never place unrequested broker orders; never cancel live orders outside
  explicit test mocks or operator request.

### The thread boundary

IBKR callbacks (`orderStatus`, `execDetails`, `commissionReport`,
`tickPrice`, `position`) arrive on the `TWSClientThread` daemon reader thread,
not the event loop. Any state touched by both must be protected by a primitive
visible to both — an `asyncio.Lock` is not.

This boundary is the origin of findings C5, C8 and C12 in the 2026-09-03
concurrency review.

### Identifier types

`AGENTS.md` §9 defines five distinct broker identifiers, and confusing them has
caused real bugs:

| Identifier | Type | Lifetime |
|---|---|---|
| `broker_order_id` | int | **Session only** — invalid across gateway restarts |
| `perm_id` | bigint | Permanent, survives restarts |
| `exec_id` | string | Unique per fill; must be processed exactly once |
| `con_id` | bigint | Contract id — CFD and underlying STK have *different* ones |
| `internal_order_id` | string | Ours (`ORD-…` / `MAN-…`), sent as `orderRef` |

### Market data

CFDs are mapped to their underlying STK contract for marks
(`market_data_con_id`). Fallback order is mid-price `(bid+ask)/2`, then previous
close. Prices are never invented and the entry price is never used as a mark
(Risk 3, `18-KNOWN-RISKS.md`). Delayed data is used automatically when
entitlements are missing (`d079d88`).

### Out-of-order callbacks

`commissionReport` can arrive before `execDetails`. The in-memory pending
commission buffer (`_pending_commissions` in `manual_callbacks.py`) exists for
this and must be preserved (Risk 5).

### Current limitations

- One rate limiter shared by all accounts (Risk 2).
- Finding C6 (P1, "certain"): `watch_open` calls the **blocking**
  `request_contract_details` on the event loop — up to ~6s per leg — stalling
  heartbeats, timeouts and HTTP handlers. **Status: not re-verified. Unknown.**
- Findings C8 (reqId increment race) and C12 (order id counter guarded by two
  different locks) are both cross-thread counter races. **Status: not
  re-verified.**

---

## Engineering History

### Chronology

| Date | Change | Commit |
|---|---|---|
| 2026-08-09 | TWS API client wrapper | `3397a11`, `ad22145` |
| 2026-08-18 | Temporary STK→CFD execution mapping for paper trading | `ab1a124` |
| 2026-08-20 | CFD-to-underlying market data mapping; subscription cooldown | `4263cb9`, `27fc479` |
| 2026-08-20 | Market data health monitoring; synthetic symbol filtering | `c4c7d8c` |
| 2026-08-21 | **IBKR execution pacing scheduler** | `f49a093`, `16afbb0` |
| 2026-08-21 | Automatic fallback to delayed market data | `d079d88` |
| 2026-09-03 | `whatIf` margin probing fixed across four commits | `a817a47` → `d9bd54a` |

### The whatIf margin sequence

On 2026-09-03 five commits in a row fixed margin probing via `Order.whatIf`.
Read in order they are a compressed lesson in integrating a third-party API:

| Commit | Problem |
|---|---|
| `a817a47` | `_WhatIfClient` wrapper property setter bug for ibapi 9.81.1 |
| `e050ddc` | Read-only wrapper property prevented `EClient.__init__` setting `wrapper=self` |
| `e186b41` | IBKR error 321 — `whatIf` orders require `transmit=True` |
| `0b72ada` | `eTradeOnly`/`firmQuoteOnly` must be `False`, matching the OMS adapter |
| `d9bd54a` | Codes 10349/10xxx are informational, not terminal |

Each fix revealed the next error underneath. The pattern is worth noting: the
library's object model (`e050ddc`), the API's validation rules (`e186b41`), the
account's contract requirements (`0b72ada`), and the error taxonomy
(`d9bd54a`) are four separate sources of failure, and they can only be
discovered in sequence against a live gateway.

The final commit is also a recurring theme: **not every error code is an
error.** Treating informational codes as terminal is its own failure mode.

### Case studies

No standalone case study. The market-data and pacing work is summarised above;
the cross-thread findings are carried as limitations in
[Chapter 21](../part4_ownership/21_current_known_limitations.md).
