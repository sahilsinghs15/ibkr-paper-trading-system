# Gaps — not implemented (verified absent)

**Verified from:** router inventory under `backend/app/api/`, `backend/app/rms/checks/`, `backend/app/main.py`, `frontend/src/`, comparison to [`Execution_System_Architecture.md`](../../Execution_System_Architecture.md) and stale guides.

This file lists things agents must **not** claim are implemented. Items appear here because they are missing from code (or only exist as unused schemas / stale docs).

## vs target architecture (`Execution_System_Architecture.md`)

| Architecture item | Code reality |
|-------------------|--------------|
| Separate Listener / Strategy / per-account OMS / Risk processes | Single FastAPI process |
| Nine RMS checks | Only checks 2, 3, 4, 7, 8 as classes |
| RMS check 1 (margin), 5, 6, 9 | No check modules |
| Dashboard config API (accounts / allocations / limits CRUD) | **Implemented** at `/api/v1/config/*` on trading app; proxied from `:8010` |
| Kill switch UI / flatten-all / health lights | Partial — `enabled` flags editable on Settings page; no flatten-all |
| Risk-engine auto exit on target / stop / time_limit | No exit-trigger loop found |
| Redis hot margin / locks / health for trading | Redis only in `demo_streaming` |
| `signal_legs` table | Not created |
| Dedicated IBKR reconciler engine as described | Not present as that process |

## vs stale product / Postman / old guide claims

| Claim | Code reality |
|-------|--------------|
| MockBroker / `BROKER_MODE` | Not in `Settings`; no MockBroker class |
| Place / modify order HTTP APIs | Schemas exist; **no** routes |
| Positions / margin / broker status HTTP APIs on `app.main` | Schemas exist; **no** routes on trading app (read-only positions live on `demo_streaming` `:8010`) |
| Five Candle live strategy engine as product path | Model Blue webhook path is what executes; candle Settings fields are unused by that path |
| React “Live Dashboard” | **Implemented** — PnL on `/` and Settings on `/settings` (Vite + `:8010`) |
| WebSocket on main app | None (dashboard uses SSE on demo process) |
| CORS on main app | None |
| Account DB / position DB “not implemented” (old DEVELOPER_EXECUTION_GUIDE bullets) | **Stale** — Postgres models and repos **do** exist; prefer this docs tree |

## Frontend gaps (remaining)

- No in-app auth on the `:8010` dashboard (restrict via AWS security group)
- Mark / last price columns stay `—` (demo payload does not fill them)
- No HTTPS / ngrok for `:8010` (optional ops choice)
- No Tailwind / lightweight-charts wiring
- Settings page does not edit target/stop/time_limit (columns exist; no exit-trigger loop)

## Live PnL / market data (residual)

- CFD `conId` discovery and upsert are implemented; Live PnL subscribes CFD contracts with `conId` when known.
- IBKR paper may still not stream CFD ticks even with a valid `conId`; there is **no** STK-underlying mark fallback in code.

## When implementing something from this list

Update the relevant `app/docs/*.md` file in the same change. Do not leave architecture or Postman text as the only description of new behavior.
