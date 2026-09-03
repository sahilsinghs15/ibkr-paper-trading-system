# Frontend

**Verified from:** `frontend/package.json`, `frontend/vite.config.ts`, `frontend/index.html`, `frontend/src/*`, `frontend/positions-demo.html`, `backend/demo_streaming/static/index.html`, `backend/demo_streaming/api.py`, `backend/demo_streaming/main.py`, `backend/app/main.py`.

## Two surfaces

| Surface | Tech | Status |
|---------|------|--------|
| `frontend/` Vite + React | React 19 + TypeScript | PnL dashboard + Settings (RMS / allocations) |
| Positions demo HTML | Vanilla HTML/JS | Fallback if `frontend/dist` is missing |

Main FastAPI (`app.main`) does **not** serve any frontend. The dashboard is served by **`demo_streaming` on `:8010`**, which proxies `/api/v1/config/*` to the trading app.

## React app (`app/frontend`)

### Entry

- `index.html` — `#root`, loads `/src/main.tsx`
- `main.tsx` — `BrowserRouter` + `QueryClientProvider` + `<App />`
- `App.tsx` — routes `/` (positions) and `/settings`

### `src/` layout

- `pages/SystemMonitorPage.tsx` — operational metrics (polls `/api/v1/system-monitor`)
- `pages/ReconcilePage.tsx` — broker vs ledger reconcile view (polls `/api/v1/reconcile/positions`)
- `api/reconcileApi.ts` — axios client for `/api/v1/reconcile/positions`
- `types/reconcile.ts` — reconcile API types
- `types/position.ts` — demo stream payload types
- `pages/AccountSettingsPage.tsx` — **routed** Settings UI (`/account/:ibkrAccount/settings`; `/settings` redirects). `SettingsPage.tsx` exists but is **not mounted**.
- `api/marginApi.ts` — axios client for `/api/v1/margin/accounts*`
- `types/margin.ts` — live snapshot + `MarginSettings` types
- `types/config.ts` — config API types (includes margin policy schemas)
- `store/pnlStore.ts` — Zustand active/closed leg maps + stream state
- `hooks/usePnlStream.ts` — `GET /demo/positions` + `EventSource("/demo/stream")` (positions route only)
- `utils/format.ts` — USD/PnL/time/instrument helpers
- `components/` — `DashboardHeader`, `AppNav`, `Kpis`, `OpenPositionsTable`, `ClosedPositionsTable`, `CriticalIncidentsBanner`
- `api/criticalBasketsApi.ts` — axios client for `/api/v1/baskets/critical`
- `types/criticalBaskets.ts` — critical incident API types
- `App.css` — demo-matching dark theme (no Tailwind)

### Positions page (`/`)

1. `GET /demo/positions` — snapshot; keep OPEN legs
2. `EventSource("/demo/stream")` — SSE updates
3. On SSE error: mark reconnecting, wait 1s, reload snapshot, reconnect
4. KPIs + open/closed tables; group by `(account_id, trade_id)`; use **one** pair `unrealized_pnl` per trade (do not sum both legs). Fifth KPI card **ACCOUNT MARGIN** polls `GET /api/v1/margin/accounts/{ibkr}` every 15s (`Kpis.tsx`). Stale or HTTP 503 renders a dimmed value with a `STALE` / `GATEWAY DOWN` pill — never show a stale figure as live. Grid is five columns (`.factory-kpis`).
5. Poll `GET /api/v1/baskets/critical?ibkr_account=` every 5s — banner + incident table when any CRITICAL basket exists; empty list means OPEN trading resumed for that account
6. NY vs IST timezone in `localStorage` key `modelBlue.displayTimezone`
7. Display maps instrument `STK` → label `CFD`

### Settings page (`/account/:ibkrAccount/settings`)

Routed component is `AccountSettingsPage.tsx`, not `SettingsPage.tsx`.

- `GET /api/v1/config/accounts` — load nested config
- Per account: edit **Trading capital** (`accounts.total_margin`; market-value budget, not IBKR margin), `enabled`, allocation `alloc_pct` (with enabled-sum ≤ 100% guard), **per-pair allocation** (`pair_max_allocation_pct`, with a derived `$X per pair · room for N pairs` hint), per-account `max_open_positions`, and `per_symbol_limits` CRUD. Broker free-margin from `GET /api/v1/margin/accounts/{ibkr}` is shown beside that input so the two figures are not confused. New allocations are created from `AccountsPage` `AddAllocationModal` (includes the per-pair field).
- Auto square-off & retry: `GET/PATCH /api/v1/config/execution`
- **Margin gate policy:** `GET/PATCH /api/v1/config/margin` (`MarginSettingsCard`) — `check_enabled` defaults true (**Margin check enabled**); uncheck for shadow mode. Comfort ratio, floors, look-ahead; no TWS restart
- Saves via PATCH/PUT/DELETE on `/api/v1/config/*` (proxied to trading app `:8001`)
- **No** Gateway host/port/clientId binding. `ibkr_account` is the IB account id tagged on orders, not a socket. Target UI: [`backend-multi-gateway.md`](backend-multi-gateway.md).

### Reconcile page (`/account/:ibkrAccount/reconcile`)

- `GET /api/v1/reconcile/positions?ibkr_account=` — latest persisted IBKR snapshot, OPEN ledger pair rows, and freshly classified diffs
- Poll every 30s (same pattern as System Monitor)
- **Differences table only** (broker vs ledger classified diffs); KPI chips and snapshot/ledger tables removed from UI
- Per-row **Square off**: `POST /api/v1/reconcile/positions/flatten` — closes the IBKR broker line only (snapshot qty); does not arm kill switch or close OPEN ledger pairs

### Scripts (`package.json`)

- `dev` → `vite` (proxies `/demo` → `:8010`, `/api/v1/config` → `:8001`)
- `build` → `tsc -b && vite build` → `frontend/dist`
- `lint` → `eslint .`
- `preview` → `vite preview`

Requires **Node.js ≥ 20** (Vite 8 / rolldown native bindings).

### `vite.config.ts`

Proxies `/demo` to `http://127.0.0.1:8010` and `/api/v1/config` to `http://127.0.0.1:8001` for local `npm run dev`. No CORS needed.

### Declared deps vs used in `src/`

| Package | Used in `src/`? |
|---------|-----------------|
| `react`, `react-dom` | Yes |
| `axios` | Yes |
| `zustand` | Yes |
| `@tanstack/react-query` | Yes (Settings) |
| `react-router-dom` | Yes |
| `lightweight-charts` | No |
| `tailwindcss` | No (plain CSS in `App.css`) |

## Serving on `:8010` (remote / server IP)

`demo_streaming` process:

```bash
cd /home/tradingapp/app/backend
# Local-only default host is 127.0.0.1
.venv/bin/python -m demo_streaming

# Reachable on the server public IP (AWS SG: inbound TCP 8010 from your IP):
DEMO_STREAM_HOST=0.0.0.0 .venv/bin/python -m demo_streaming
# http://PUBLIC_IP:8010/
# Settings: http://PUBLIC_IP:8010/settings
```

Defaults: port `8010`, Redis `redis://127.0.0.1:6379/0`, Postgres via `DATABASE_URL`, `trading_api_url` `http://127.0.0.1:8001`, poll `2000` ms, PnL SSE coalesce `5000` ms (one event per trade). Does **not** connect to IBKR for market data. Config writes proxy to the trading app.

`GET /` and `GET /settings` serve `frontend/dist/index.html` when present (after `npm run build`), and mount `frontend/dist/assets` at `/assets`. Otherwise falls back to `demo_streaming/static/index.html`.

Keep TradingView / ngrok on webhook ingest **`:8000` only**. Do not bind the trading app to `0.0.0.0`.

See also [`../start.txt`](../../start.txt).

## Other HTML

`frontend/ibkr-tws-price-streaming-guide.html` — standalone TWS API documentation article. Not wired to FastAPI or Vite routing.

## Related docs

- Demo + config API: [`backend-api.md`](backend-api.md)
- Remaining gaps: [`gaps.md`](gaps.md)
