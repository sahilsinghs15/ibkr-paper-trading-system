# Agent map — IBKR paper trading app

**Verified from:** `backend/app/main.py`, `backend/app/api/routes/*`, `backend/app/core/config.py`, `backend/demo_streaming/*`, `frontend/src/*`.

One FastAPI process ingests TradingView webhooks into a durable Postgres queue (`signal_jobs`), executes them via a 10-worker pool through Model Blue → RMS (checks 2/3/4/7/8) → basket OMS → IBKR TWS, with execution claims, crash recovery, and kill-switch support. The Vite React app under `frontend/` is a live PnL dashboard plus a **Settings** page for RMS limits and capital allocation. It is served by a separate demo SSE process on port **8010** (not by `app.main`).

Do **not** treat [`../Execution_System_Architecture.md`](../Execution_System_Architecture.md) as current code. Do **not** use [`backend/POSTMAN_API_TESTING_GUIDE.md`](backend/POSTMAN_API_TESTING_GUIDE.md) as an API inventory (it documents endpoints that do not exist).

## Which doc to open

| Task | Doc |
|------|-----|
| Debug a missed / rejected / incomplete fill | [`docs/backend-execution.md`](docs/backend-execution.md) |
| Package tree / where to change code | [`docs/backend-map.md`](docs/backend-map.md) |
| Jobs, workers, leases, claims, recovery | [`docs/backend-concurrency.md`](docs/backend-concurrency.md) |
| Kill switch / emergency flatten | [`docs/backend-kill-switch.md`](docs/backend-kill-switch.md) |
| Exact HTTP endpoints | [`docs/backend-api.md`](docs/backend-api.md) |
| Env / Settings fields | [`docs/backend-config.md`](docs/backend-config.md) |
| Tables, repos, in-memory vs DB | [`docs/backend-persistence.md`](docs/backend-persistence.md) |
| RMS checks / basket / TWS adapter | [`docs/backend-rms-oms.md`](docs/backend-rms-oms.md) |
| pytest / ruff | [`docs/backend-testing.md`](docs/backend-testing.md) |
| React PnL dashboard + demo UI | [`docs/frontend.md`](docs/frontend.md) |
| How to add a route / DI rules | [`docs/conventions.md`](docs/conventions.md) |
| Paper vs live ports, STK→CFD | [`docs/safety.md`](docs/safety.md) |
| What is **not** implemented | [`docs/gaps.md`](docs/gaps.md) |
| Doc index | [`docs/README.md`](docs/README.md) |

## Run / test

```bash
# Main trading API (default docs use 127.0.0.1:8000)
cd /home/tradingapp/app/backend
uv sync --extra dev   # or use existing .venv
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000

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
```

Runtime logs for the main app: `/home/tradingapp/storage/logs/trading-YYYY-MM-DD.log` (daily midnight rollover via `app/core/logger.py`). Demo stream: `storage/logs/demo-YYYY-MM-DD.log`.

## Hard invariants

- `POST /api/webhooks/tradingview` returns **HTTP 202** with status **`accepted`** and enqueues `signal_jobs`; workers run `process_signal_execution` asynchronously. Also writes disk capture.
- Lifespan: TWS → OMS → OrderManager → hydrate → connect → **RecoveryManager** → **ExecutionWorkerPool(10)** on `app.state.worker_pool`.
- **Execution claims** are acquired after RMS + instrument resolve, before broker submit — the durable dedupe barrier across crashes/workers.
- Kill switch **stays armed** after flatten completes until operator `POST .../kill-switch/clear`.
- Paper basket retries only on IBKR ports `{7497, 4002}`. Live ports are **not** rejected for ordinary trading.
- Production submit pacing is `OrderSubmitPacer(0.2s)` — not `IBKRExecutionScheduler` (tests-only).
- Main app HTTP surface: health, webhook, orders, and **config CRUD** under `/api/v1/config/*`. No CORS, WebSocket, or static files on `app.main`.
- Default IBKR port is `7497` (paper TWS).
- There is **no** `BROKER_MODE` / MockBroker in `Settings`. Extra env keys are ignored (`extra="ignore"`).
- Redis is used only by `demo_streaming`, not by the main trading app.
- Do not bind `app.main` / ngrok to expose the dashboard; use `:8010` (optionally `DEMO_STREAM_HOST=0.0.0.0`).

## Ignore / do not treat as source of truth

| Path | Why |
|------|-----|
| `backend/POSTMAN_API_TESTING_GUIDE.md` | Historical; lists APIs and schemas that no longer exist |
| `backend/docs/DEVELOPER_EXECUTION_GUIDE.md` | Stale human guide — prefer `app/docs/` |
| `../Execution_System_Architecture.md` | Target design, not current code |
| `broker/ibkr/scheduler.py` as live pacing | Tests-only; production uses `OrderSubmitPacer` |

## Source of truth (code)

- Entrypoint: `backend/app/main.py`
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
