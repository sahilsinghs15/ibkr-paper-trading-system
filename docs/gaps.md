# Gaps — not implemented (verified absent)

**Verified from:** router inventory under `backend/app/api/`, `backend/app/rms/checks/`, `backend/app/main.py`, `frontend/src/`, comparison to [`Execution_System_Architecture.md`](../../Execution_System_Architecture.md) and stale guides.

This file lists things agents must **not** claim are implemented. Items appear here because they are missing from code (or only exist as unused schemas / stale docs).

## vs target architecture (`Execution_System_Architecture.md`)

| Architecture item | Code reality |
|-------------------|--------------|
| Separate Listener / Strategy / per-account OMS / Risk processes | **Partial** — webhook ingest is a separate process (`app.webhook_ingest:app` on `:8000`); strategy/OMS/risk still in-process on trading app `:8001` |
| Nine RMS checks | Checks 1, 2, 3, 4, 7, 8 plus local 101 (model market value). 5, 6, 9 remain unbuilt |
| RMS check 1 (margin) | **Implemented** — `MarginCheck` + Gate A/C; ships `check_enabled=true`. Uncheck on Settings for shadow (`MARGIN_CHECK_DISABLED`). Check 101 does **not** satisfy check 1: 101 is gross market value, not broker margin. `orders.margin_impact` is still unpopulated except from broker-derived rates. Checks 5, 6, 9 still missing |
| N IB Gateway instances / account→gateway routing | **Not built.** Multi-account today = `ib_order.account` on **one** socket. Target: [`backend-multi-gateway.md`](backend-multi-gateway.md) |
| Per-gateway rate limiter (token bucket / fairness / Error 100) | **Partial.** One `GatewayRateLimiter` on the single socket (~30/24/6, P0 reserve, Error 100 cooldown). Not per-gateway, not fair across accounts |
| TWS reconnect / failover | **Partial.** `TWSClient` reconnects the **one** socket on `connectionClosed`. No second host / clientId rotation. Parked baskets wait for reconnect; they are not compensated on disconnect. See [`runbooks/gateway-failure.md`](runbooks/gateway-failure.md) |
| Dashboard config API (accounts / allocations / limits CRUD) | **Implemented** at `/api/v1/config/*` on trading app; proxied from `:8010`. Does **not** bind accounts to Gateways |
| Kill switch / flatten-all | **Partial** — HTTP API exists (`POST .../square-off`, clear, status); see [`backend-kill-switch.md`](backend-kill-switch.md). Dashboard UX may not expose all controls — verify frontend before claiming UI. |
| `IBKRExecutionScheduler` / `OrderSubmitPacer` | **Removed** — replaced by `GatewayRateLimiter` |
| Risk-engine auto exit on target / stop / time_limit | No exit-trigger loop found |
| Redis hot margin / locks / health for trading | Redis only in `demo_streaming`. Live headroom is an in-process snapshot + running tally on `RMSContext` (same property as `symbol_exposures`); `margin_rates` / `margin_settings` are durable |
| `signal_legs` table | Not created |
| Dedicated IBKR reconciler engine as described | **Partial** — in-process `PositionReconciler` snapshots IBKR lines to `broker_positions`, diffs vs OPEN `positions`, logs to `event_log` / `position_reconcile_runs`. Dashboard at `/account/:ibkrAccount/reconcile` via `GET /api/v1/reconcile/positions`; per-row broker flatten via `POST /api/v1/reconcile/positions/flatten` (no ledger repair, no kill switch) |

## vs stale product / Postman / old guide claims

| Claim | Code reality |
|-------|--------------|
| MockBroker / `BROKER_MODE` | Not in `Settings`; no MockBroker class |
| Place / modify order HTTP APIs | Schemas exist; **no** routes |
| Positions / margin / broker status HTTP APIs on `app.main` | Live margin: `GET /api/v1/margin/accounts` and `GET /api/v1/margin/accounts/{ibkr_account}` (503 if gateway down). Positions remain read-only on `demo_streaming` `:8010`. Place/modify order routes still absent |
| Five Candle live strategy engine as product path | Model Blue webhook path is what executes; candle Settings fields are unused by that path |
| Webhook runs pipeline synchronously in HTTP handler | **Stale** — normal path enqueues `signal_jobs`; workers execute (HTTP 202 `accepted`) |
| React “Live Dashboard” | **Implemented** — PnL on `/` and Settings on `/settings` (Vite + `:8010`) |
| WebSocket on main app | None (dashboard uses SSE on demo process) |
| CORS on main app | None |
| Account DB / position DB “not implemented” (old DEVELOPER_EXECUTION_GUIDE bullets) | **Stale** — Postgres models and repos **do** exist |

## Frontend gaps (remaining)

- No in-app auth on the `:8010` dashboard (restrict via AWS security group)
- Mark / last price columns may stay `—` when IBKR paper does not stream CFD ticks
- No HTTPS / ngrok for `:8010` (optional ops choice)
- No Tailwind / lightweight-charts wiring in all views
- Settings page may not expose kill-switch square-off / clear (API exists; verify UI)
- Settings page does not edit target/stop/time_limit exit automation (columns exist; no exit-trigger loop)

## Live PnL / market data (residual)

- CFD `conId` discovery and upsert are implemented; Live PnL subscribes CFD contracts with `conId` when known.
- IBKR paper may still not stream CFD ticks even with a valid `conId`; there is **no** STK-underlying mark fallback in code.

## Multi-gateway / rate limiting (target, not as-is)

Design intent for N Gateways, per-gateway limiter, mapping policy, fairness, and failure semantics lives in [`backend-multi-gateway.md`](backend-multi-gateway.md). Do not describe that file as current behavior.

Short list of **MISSING** items (citations in that file):

- `gateways` / `account_gateway_bindings` tables and config API
- `GatewayPool` / `GatewayRouter`
- Per-gateway limiter shared by all accounts on that instance
- Account-scoped market-data line cap (not a gateway limiter)
- Reconnect without ERROR-ing in-flight OMS orders
- Per-account `signal_jobs` / `account_scope` on ingest

## When implementing something from this list

Update the relevant `app/docs/*.md` file in the same change. Do not leave architecture or Postman text as the only description of new behavior. If you implement any multi-gateway item, update [`backend-multi-gateway.md`](backend-multi-gateway.md) as-is sections in the same PR.
