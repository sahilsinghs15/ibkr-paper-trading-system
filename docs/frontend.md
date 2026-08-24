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

- `pages/PositionsPage.tsx` — PnL view (header, KPIs, tables)
- `pages/SettingsPage.tsx` — account margin, allocations, per-symbol limits
- `api/configApi.ts` — axios client for `/api/v1/config/*`
- `types/position.ts` — demo stream payload types
- `types/config.ts` — config API types
- `store/pnlStore.ts` — Zustand active/closed leg maps + stream state
- `hooks/usePnlStream.ts` — `GET /demo/positions` + `EventSource("/demo/stream")` (positions route only)
- `utils/format.ts` — USD/PnL/time/instrument helpers
- `components/` — `DashboardHeader`, `AppNav`, `Kpis`, `OpenPositionsTable`, `ClosedPositionsTable`
- `App.css` — demo-matching dark theme (no Tailwind)

### Positions page (`/`)

1. `GET /demo/positions` — snapshot; keep OPEN legs
2. `EventSource("/demo/stream")` — SSE updates
3. On SSE error: mark reconnecting, wait 1s, reload snapshot, reconnect
4. KPIs + open/closed tables; group by `(account_id, trade_id)`; use **one** pair `unrealized_pnl` per trade (do not sum both legs)
5. NY vs IST timezone in `localStorage` key `modelBlue.displayTimezone`
6. Display maps instrument `STK` → label `CFD`

### Settings page (`/settings`)

- `GET /api/v1/config/accounts` — load nested config
- Per account: edit `total_margin`, `enabled`, allocation `alloc_pct` (with enabled-sum ≤ 100% guard), per-account `max_open_positions`, and `per_symbol_limits` CRUD
- Auto square-off & retry: `GET/PATCH /api/v1/config/execution`
- Saves via PATCH/PUT/DELETE on `/api/v1/config/*` (proxied to `:8000`)
- **No** Gateway host/port/clientId binding. `ibkr_account` is the IB account id tagged on orders, not a socket. Target UI: [`backend-multi-gateway.md`](backend-multi-gateway.md).

### Scripts (`package.json`)

- `dev` → `vite` (proxies `/demo` → `:8010`, `/api/v1/config` → `:8000`)
- `build` → `tsc -b && vite build` → `frontend/dist`
- `lint` → `eslint .`
- `preview` → `vite preview`

Requires **Node.js ≥ 20** (Vite 8 / rolldown native bindings).

### `vite.config.ts`

Proxies `/demo` to `http://127.0.0.1:8010` and `/api/v1/config` to `http://127.0.0.1:8000` for local `npm run dev`. No CORS needed.

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

Defaults: port `8010`, Redis `redis://127.0.0.1:6379/0`, Postgres via `DATABASE_URL`, `trading_api_url` `http://127.0.0.1:8000`, poll `2000` ms. Does **not** connect to IBKR for market data. Config writes proxy to the trading app.

`GET /` and `GET /settings` serve `frontend/dist/index.html` when present (after `npm run build`), and mount `frontend/dist/assets` at `/assets`. Otherwise falls back to `demo_streaming/static/index.html`.

Keep TradingView / ngrok on **`:8000` only**. Do not bind `app.main` to `0.0.0.0`.

See also [`../start.txt`](../../start.txt).

## Other HTML

`frontend/ibkr-tws-price-streaming-guide.html` — standalone TWS API documentation article. Not wired to FastAPI or Vite routing.

## Related docs

- Demo + config API: [`backend-api.md`](backend-api.md)
- Remaining gaps: [`gaps.md`](gaps.md)
