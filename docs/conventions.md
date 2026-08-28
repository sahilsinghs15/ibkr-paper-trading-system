# Conventions for agents

**Verified from:** `backend/app/main.py`, `backend/app/webhook_ingest.py`, `backend/app/api/router.py`, `backend/app/api/deps.py`, `backend/app/core/config.py`, `backend/app/core/identifiers.py`, `backend/app/services/worker_pool.py`, `frontend/package.json`, `frontend/vite.config.ts`.

## Backend

### Dependency injection

- Wire long-lived services in `lifespan` on `app.state` (`session_factory`, `client`, `ibkr_adapter`, `oms`, `order_manager`, `worker_pool`).
- There is **one** `TWSClient`. Do not construct another in a request handler or worker. A second client with the same `IBKR_CLIENT_ID` disconnects the first; an unpaced client bypasses `GatewayRateLimiter`.
- Route deps use helpers such as `get_oms` in `app/api/deps.py`.
- Do not construct a second OMS / OrderManager / worker pool inside a request handler.

### Config

- Read settings via `get_settings()`.
- Do not reintroduce `BROKER_MODE` / MockBroker unless you implement both the setting and the broker path in code.
- Unknown env keys are ignored (`extra="ignore"`); documenting them as Settings fields is wrong.
- Worker count, lease durations — hardcoded in `main.py` / `worker_pool.py`. Gateway limiter knobs are Settings fields (`IBKR_GATEWAY_*`). See [`backend-map.md`](backend-map.md).

### Identifiers

- Use `normalize_strategy_id()` for any persisted strategy key or idempotency input — lowercase, trimmed.
- Use `normalize_trade_id()` for trade identifiers — case preserved.
- Changing normalization requires migration/backfill (see `a4c7e2f10938`).

### Adding an HTTP route

1. Add a router module under `app/api/routes/`.
2. Include it from `app/api/router.py` (for `/api/v1/...`) or from `webhook_ingest.create_ingest_app()` / `main.create_app()` with an explicit prefix (webhooks under `/api` on ingest only).
3. Prefer Pydantic schemas for request/response; keep domain models as dataclasses where the codebase already does.
4. Do not claim an endpoint exists until a router decorator is present (see [`backend-api.md`](backend-api.md)).

### Logging

- `logging.getLogger(__name__)`.
- Prefer `%s` formatting (existing style).
- Call `setup_logging` once at startup; daily files under workspace `storage/logs/{YYYY-MM-DD}/{prefix}.log` (e.g. `trading.log`, `webhook.log`, `demo.log`).
- Correlation via `bind_log_context` / `clear_log_context` (`request_id`, `signal_id`, `trade_id`, `account_id`) → `%(trace)s` on every line.

### Persistence

- Durable state → Postgres models / repositories.
- Do not assume Redis is available on the main trading path (demo-only).
- Run `app.main` as **one process** (do not `uvicorn --workers N` against the same Gateway). The live limiter is in-process (`GatewayRateLimiter`). Multiple workers each get a bucket and can exceed IB’s ~50 msg/sec. Target policy: [`backend-multi-gateway.md`](backend-multi-gateway.md).

### Concurrency / execution

- Webhook ingest enqueues `signal_jobs` on `:8000`; trading workers execute on `:8001`. Do not make synchronous execution the default when the pool is running.
- Job status writes must be lease-fenced (`worker_id` + live lease).
- Execution claims: acquire after RMS + resolve, before `placeOrder`; seal on settled basket; release only if zero orders emitted.
- Never requeue a job that already has order rows — use `RECOVERY_REQUIRED`.
- Domain lock `(account_scope, strategy_id)` and exposure lock `(account_id, symbol)` serve different purposes — keep both.

### Kill switch

- Armed state survives restart — always hydrate from DB on startup.
- Completing flatten does not disarm; only explicit clear API.
- DB write before cache clear.

## Frontend

- Prefer already-declared packages (`axios`, `@tanstack/react-query`, `zustand`, `react-router-dom`, `lightweight-charts`, `tailwindcss`) before adding new dependencies.
- Live PnL UI lives in `frontend/src` and is served by `demo_streaming` on `:8010` (Vite proxies `/demo` in dev).
- Do not add browser WebSocket clients against `app.main` unless you also implement WebSocket (or SSE) on the backend; today `app.main` has neither CORS nor WS. Dashboard SSE is on the demo process only.

## Documentation hygiene

- Current facts live under `app/docs/`.
- [`Execution_System_Architecture.md`](../../Execution_System_Architecture.md) is target design (parent dir, not this repo).
- Multi-gateway / per-gateway rate-limit **target** (as-is vs plan): [`backend-multi-gateway.md`](backend-multi-gateway.md).
- Postman guide and `DEVELOPER_EXECUTION_GUIDE.md` are historical — do not copy their endpoint lists into new docs or code comments as "implemented".
- When implementing something listed in [`gaps.md`](gaps.md), update the relevant `app/docs/*.md` in the same change.
