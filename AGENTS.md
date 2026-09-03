# Agent map — IBKR paper trading app

**Verified from:** `backend/app/main.py`, `backend/app/webhook_ingest.py`, `backend/app/api/routes/*`, `backend/app/core/config.py`, `backend/demo_streaming/*`, `frontend/src/*`.

Two FastAPI processes: **webhook ingest** (`app.webhook_ingest:app` on `:8000`, Postgres-only) receives TradingView alerts into `signal_jobs`; **trading/execution** (`app.main:app` on `:8001`) runs a 10-worker pool through Model Blue → RMS (checks 1/2/3/4/7/8/101) → basket OMS → IBKR TWS, with execution claims, crash recovery, and kill-switch support. Ingest stays up when Gateway bounces or the trading app restarts. Multi-account routing tags `ib_order.account` on **one** TWS/Gateway socket (`GatewayRateLimiter` ~30 msg/sec, P0 flatten reserve). N IB Gateways and per-gateway rate limits are **not** built — [`docs/backend-multi-gateway.md`](docs/backend-multi-gateway.md). The Vite React app under `frontend/` is a live PnL dashboard plus a **Settings** page for RMS limits and capital allocation. It is served by a separate demo SSE process on port **8010** (not by `app.main`).

Do **not** treat [`../Execution_System_Architecture.md`](../Execution_System_Architecture.md) as current code. Do **not** use [`backend/POSTMAN_API_TESTING_GUIDE.md`](backend/POSTMAN_API_TESTING_GUIDE.md) as an API inventory (it documents endpoints that do not exist).

## Which doc to open

| Task | Doc |
|------|-----|
| Debug a missed / rejected / incomplete fill | [`docs/backend-execution.md`](docs/backend-execution.md) |
| Package tree / where to change code | [`docs/backend-map.md`](docs/backend-map.md) |
| Jobs, workers, leases, claims, recovery | [`docs/backend-concurrency.md`](docs/backend-concurrency.md) |
| Kill switch / emergency flatten / IBKR leftover flatten script | [`docs/backend-kill-switch.md`](docs/backend-kill-switch.md) |
| Gateway socket drop / reconnect / parked baskets | [`docs/runbooks/gateway-failure.md`](docs/runbooks/gateway-failure.md) |
| Exact HTTP endpoints | [`docs/backend-api.md`](docs/backend-api.md) |
| Env / Settings fields | [`docs/backend-config.md`](docs/backend-config.md) |
| Tables, repos, in-memory vs DB | [`docs/backend-persistence.md`](docs/backend-persistence.md) |
| RMS checks / basket / TWS adapter | [`docs/backend-rms-oms.md`](docs/backend-rms-oms.md) |
| Multi-account vs multi-gateway / rate limits | [`docs/backend-multi-gateway.md`](docs/backend-multi-gateway.md) |
| pytest / ruff | [`docs/backend-testing.md`](docs/backend-testing.md) |
| React PnL dashboard + demo UI | [`docs/frontend.md`](docs/frontend.md) |
| How to add a route / DI rules | [`docs/conventions.md`](docs/conventions.md) |
| Paper vs live ports, STK→CFD | [`docs/safety.md`](docs/safety.md) |
| What is **not** implemented | [`docs/gaps.md`](docs/gaps.md) |
| Watchdog (monitoring, Telegram, recovery) | [`docs/watchdog.md`](docs/watchdog.md) |
| Doc index | [`docs/README.md`](docs/README.md) |

## Run / test

```bash
# Webhook ingest (TradingView / ngrok — Postgres only, no IBKR)
cd /home/tradingapp/app/backend
uv sync --extra dev   # or use existing .venv
.venv/bin/uvicorn app.webhook_ingest:app --host 127.0.0.1 --port 8000

# Trading / execution API (local only)
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8001

# Tests / lint
.venv/bin/pytest
.venv/bin/ruff check app/ tests/ scripts/

# Read-only PnL dashboard (needs Postgres + Redis; Node ≥20 to build React)
.venv/bin/python -m demo_streaming
# open http://127.0.0.1:8010/
# remote: DEMO_STREAM_HOST=0.0.0.0 … then http://PUBLIC_IP:8010/ (SG TCP 8010)

# Vite React dashboard (proxies /demo → :8010)
cd /home/tradingapp/app/frontend
npm install && npm run build   # optional: serve dist from :8010
npm run dev                    # http://127.0.0.1:5173/

# Production run path is systemd units (not process_manager.py):
#   trading-backend.service  (:8001)
#   webhook-ingest.service   (:8000)
#   ibgateway.service
# process-manager.service must stay disabled.
```

Runtime logs: trading app `storage/logs/{YYYY-MM-DD}/trading.log`; webhook ingest `webhook.log`; demo stream `demo.log`. Do not write `/home/tradingapp/logs`.

## Hard invariants

- `POST /api/webhooks/tradingview` on **ingest** (`:8000`) returns **HTTP 202** with status **`accepted`** and enqueues `signal_jobs`; workers on **trading** (`:8001`) run `process_signal_execution` asynchronously. Durable payload is `signal_jobs.capture_data`.
- Trading app lifespan: TWS → OMS → OrderManager → hydrate → **CriticalRecoveryService** (wired to `BasketCoordinator`) → connect → **RecoveryManager** → **ExecutionWorkerPool(10)** on `app.state.worker_pool`.
- **Execution claims** are acquired after RMS + instrument resolve, before broker submit — the durable dedupe barrier across crashes/workers.
- Kill switch **stays armed** after flatten completes until operator `POST .../kill-switch/clear`.
- Remainder-retry is allowed on live Gateway **4001** after M9/M14 identity/persist fixes (`paper_retry_ports_allowed` includes 4001).
- **BASKET_CRITICAL** auto-recovery: background flatten via `CriticalRecoveryService`, unlock OPENs when broker snapshot flat (`BasketState.RECOVERED`); dashboard banner via `GET /api/v1/baskets/critical`.
- Production submit pacing is `GatewayRateLimiter` (~30/24/6 msg/sec, wait+timeout, Error 100 cooldown) on the single adapter — one limiter, one socket, all accounts. P1 `placeOrder`/`cancelOrder` may consume tokens without a normal-bucket token if that would not eat the P0 emergency reserve.
- Multi-Gateway pool / per-gateway limiter: **not implemented**. Socket reconnect-on-drop **is** implemented on the single TWS client. Do not describe `ibkr_account` as a Gateway mapping.
- Trading app HTTP surface (`:8001`): health, orders, and **config CRUD** under `/api/v1/config/*`. Webhooks live on ingest (`:8000`) only. No CORS, WebSocket, or static files on either app.
- Default IBKR port is **4001** (live Gateway).
- There is **no** `BROKER_MODE` / MockBroker in `Settings`. Extra env keys are ignored (`extra="ignore"`).
- Redis is used only by `demo_streaming`, not by the main trading app.
- Do not bind the trading app to `0.0.0.0`. Keep ngrok on ingest `:8000` only; use `:8010` for the dashboard (optionally `DEMO_STREAM_HOST=0.0.0.0`).
- `TRADINGAPP_TESTING=1` is pytest-only. Never set it on trading-backend or any other `placeOrder` process.

## Ignore / do not treat as source of truth

| Path | Why |
|------|-----|
| `backend/POSTMAN_API_TESTING_GUIDE.md` | Historical; lists APIs and schemas that no longer exist |
| `backend/docs/DEVELOPER_EXECUTION_GUIDE.md` | Stale human guide — prefer `app/docs/` |
| `../Execution_System_Architecture.md` | Target design, not current code |
| `broker/ibkr/scheduler.py` | **Removed** — live pacing is `GatewayRateLimiter` in `broker/ibkr/gateway_rate_limiter.py` |

## Source of truth (code)

- Entrypoints: `backend/app/webhook_ingest.py` (ingest), `backend/app/main.py` (trading)
- Config: `backend/app/core/config.py`
- Routes: `backend/app/api/routes/{health,webhooks,orders,config}.py`
- Queue/workers: `backend/app/services/worker_pool.py`
- Pipeline: `backend/app/services/order_manager.py`
- Recovery: `backend/app/services/recovery.py`
- Kill switch: `backend/app/services/kill_switch.py`
- RMS: `backend/app/rms/engine.py` + `backend/app/rms/checks/`
- OMS / basket: `backend/app/oms/`
- DB models: `backend/app/db/models/`
- Demo UI server: `backend/demo_streaming/`
