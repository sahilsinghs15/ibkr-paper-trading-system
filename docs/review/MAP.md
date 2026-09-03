# System map — IBKR pair-trading execution system

**Purpose:** a factual map of the code as it exists, for reviewers. No critique, no
recommendations. Every claim is cited as `file:line` relative to `/home/tradingapp/app`.

**Read at:** commit state of 2026-09-03. Paths are relative to the repo root
(`/home/tradingapp/app`), so `backend/app/main.py:35` means
`/home/tradingapp/app/backend/app/main.py` line 35.

## Corrections to the brief

Four things in the review brief do not match the code. Stating them up front because
several later sections would otherwise read as if something were missing.

- **"multiple IB Gateway instances"** — there is one `TWSClient` socket, constructed once
  at `backend/app/main.py:45` and connected once at `backend/app/main.py:105`. There is no
  gateway pool, no gateway selection step, and no reconnect-on-drop loop. Multi-account
  routing is done by tagging `ib_order.account` on that single socket
  (`backend/app/oms/ibkr_adapter.py:240-242`).
- **"per-gateway rate limiting"** — `GatewayRateLimiter`
  (`backend/app/broker/ibkr/gateway_rate_limiter.py:44`) takes a `gateway_id` parameter
  (`:60`) but exactly one instance is created (`backend/app/main.py:46`) and shared by the
  adapter and the client. It is one limiter for one socket across all accounts.
- **"Redis"** — Redis is used only by the read-only dashboard process
  (`backend/demo_streaming/main.py:36`, `backend/demo_streaming/stream.py:7`). There is no
  Redis import anywhere under `backend/app/`.
- **"pair-level P&L from our own fill ledger"** — partially true. Realised P&L on close is
  computed from broker fill prices (`backend/app/db/repositories/position_repository.py:204-223`)
  fed by the `executions` ledger (`backend/app/services/model_blue/persistence.py:29-38`).
  Live/unrealised P&L is *not* from the fill ledger — it comes from streaming IBKR market
  data ticks (`backend/app/services/pnl.py:350`, `:728`).

---

## 1. Entrypoints

There are five independently-launched processes. They share the `backend/app` package but
do not share memory.

### 1.1 Process inventory

| Process | Entry | Port | Launched by |
|---|---|---|---|
| Webhook ingest | `backend/app/webhook_ingest.py:64` (`app`) | 8000 | `deploy/systemd/webhook-ingest.service`, `scripts/process_manager.py` |
| Trading / execution | `backend/app/main.py:252` (`app`) | 8001 | `deploy/systemd/trading-backend.service`, `scripts/process_manager.py` |
| Demo streaming dashboard | `backend/demo_streaming/main.py` via `backend/demo_streaming/__main__.py` | 8010 | `deploy/systemd/demo-streaming.service` |
| Watchdog | `backend/scripts/watchdog_main.py` | — | `deploy/systemd/watchdog.service:12` |
| Process supervisor | `scripts/process_manager.py` | — | `deploy/systemd/process-manager.service:12` |

### 1.2 HTTP routes — webhook ingest (`:8000`)

App built at `backend/app/webhook_ingest.py:37`; routers mounted at `:58` (health, no
prefix) and `:59` (webhooks, prefix `/api`).

| Method | Path | Handler | Sync/async | File:line |
|---|---|---|---|---|
| GET | `/health` | `get_health` | async | `backend/app/api/routes/health.py:8` |
| GET | `/health/live` | `get_liveness` | async | `backend/app/api/routes/health.py:14` |
| GET | `/health/ready` | `get_readiness` | async | `backend/app/api/routes/health.py:20` |
| POST | `/api/webhooks/tradingview` | `receive_tradingview_webhook` | async | `backend/app/api/routes/webhooks.py:167` |

Trigger for the webhook route: an inbound HTTP POST from TradingView (in production via
ngrok). Auth is a constant-time compare of the `X-Webhook-Secret` header at
`backend/app/api/routes/webhooks.py:144`, called first thing in the handler at `:176`.
It is a plain function call, not a FastAPI dependency.

### 1.3 HTTP routes — trading / execution (`:8001`)

App built at `backend/app/main.py:223`; health router mounted at root (`:246`) and
`api_router` under `/api/v1` (`:247`). `api_router` composition:
`backend/app/api/router.py:17-25`.

Webhooks are **not** mounted on this app — `backend/app/main.py:245` says so explicitly and
`backend/app/api/router.py` does not import the webhooks router.

37 routes, every handler `async def` — there are no plain `def` handlers on any app in the
repo, so nothing runs in FastAPI's threadpool. Paths below are the effective URL after both
the router's own prefix and the `/api/v1` mount. "Decorator" is the `@router.*` line.

| Method | Path | Handler | Auth | Decorator |
|---|---|---|---|---|
| GET | `/health`, `/health/live`, `/health/ready` | as §1.2 | none | `health.py:8`, `:14`, `:20` |
| POST | `/api/v1/auth/login` | `login` | none | `auth.py:49` |
| POST | `/api/v1/auth/sse-token` | `get_sse_token` | authenticated | `auth.py:99` |
| GET | `/api/v1/auth/me` | `me` | authenticated | `auth.py:112` |
| GET | `/api/v1/orders` | `get_orders` | authenticated | `orders.py:17` |
| GET | `/api/v1/orders/{order_id}` | `get_order_by_id` | authenticated | `orders.py:34` |
| DELETE | `/api/v1/orders/{order_id}` | `cancel_order` | authenticated | `orders.py:55` |
| GET | `/api/v1/baskets/critical` | `list_critical_baskets` | DB session dep only | `baskets.py:80` |
| GET | `/api/v1/config/accounts` | `list_accounts_config` | authenticated | `config.py:68` |
| GET | `/api/v1/config/accounts/by-identifier/{ibkr_account}` | `get_account_by_identifier` | authenticated | `config.py:133` |
| POST | `/api/v1/config/accounts/{account_id}/square-off` | `square_off_account_positions` | authenticated | `config.py:181` |
| POST | `/api/v1/config/accounts/{account_id}/kill-switch/clear` | `clear_account_kill_switch_endpoint` | authenticated | `config.py:226` |
| GET | `/api/v1/config/accounts/{account_id}/kill-switch` | `get_account_kill_switch_status` | authenticated | `config.py:271` |
| POST | `/api/v1/config/accounts/{account_id}/positions/{trade_id}/close` | `close_selected_pair_endpoint` | authenticated | `config.py:295` |
| POST | `/api/v1/config/accounts` | `create_account` | **admin** | `config.py:325` |
| PATCH | `/api/v1/config/accounts/{account_id}` | `patch_account` | authenticated | `config.py:370` |
| POST | `/api/v1/config/accounts/{account_id}/allocations` | `create_account_allocation` | **admin** | `config.py:446` |
| GET | `/api/v1/config/accounts/{account_id}/deletable` | `check_account_deletable_api` | authenticated | `config.py:489` |
| DELETE | `/api/v1/config/accounts/{account_id}` | `delete_account_api` | **admin** | `config.py:513` |
| PATCH | `/api/v1/config/allocations/{allocation_id}` | `patch_allocation` | authenticated | `config.py:546` |
| PUT | `/api/v1/config/accounts/{account_id}/symbol-limits/{symbol}` | `put_symbol_limit` | authenticated | `config.py:592` |
| PUT | `/api/v1/config/accounts/{account_id}/default-symbol-limit` | `put_default_symbol_limit` | authenticated | `config.py:627` |
| DELETE | `/api/v1/config/accounts/{account_id}/symbol-limits/{symbol}` | `delete_symbol_limit` | authenticated | `config.py:682` |
| GET | `/api/v1/config/execution` | `get_execution_settings` | authenticated | `config.py:718` |
| PATCH | `/api/v1/config/execution` | `patch_execution_settings` | **admin** | `config.py:734` |
| GET | `/api/v1/config/margin` | `get_margin_settings` | authenticated | `config.py:794` |
| PATCH | `/api/v1/config/margin` | `patch_margin_settings` | **admin** | `config.py:809` |
| GET | `/api/v1/margin/accounts` | `list_account_margins` | **admin** | `margin.py:85` |
| GET | `/api/v1/margin/accounts/{ibkr_account}` | `get_account_margin` | authenticated | `margin.py:108` |
| POST | `/api/v1/emergency-kill-switch` | `emergency_kill_switch_endpoint` | **separate secret header** | `emergency.py:76` |
| GET | `/api/v1/system-monitor` | `get_system_monitor` | **admin** | `system_monitor.py:17` |
| GET | `/api/v1/reconcile/positions` | `get_reconcile_positions` | authenticated | `reconcile.py:23` |
| POST | `/api/v1/reconcile/positions/flatten` | `flatten_broker_position_line` | authenticated | `reconcile.py:51` |
| POST | `/api/v1/service-control/{service}/{action}` | `control_service` | **admin** | `service_control.py:34` |
| GET | `/api/v1/service-control/allowed` | `list_allowed` | **admin** | `service_control.py:99` |

Two things worth noting from that table: `GET /api/v1/baskets/critical`
(`baskets.py:80`) takes a DB-session dependency but no user dependency, and
`POST /api/v1/emergency-kill-switch` bypasses JWT entirely in favour of its own header
check (`emergency.py:28`, invoked inside the handler).

Auth for the rest of this app is JWT bearer, resolved through
`backend/app/api/deps.py:56` (`get_current_user`), with role gate at
`backend/app/api/deps.py:112` (`require_admin`). Token is accepted from the
`Authorization` header **or** a `?token=` query parameter
(`backend/app/api/deps.py:39-53`). When `TRADINGAPP_TESTING=1` an unauthenticated request
is silently promoted to a synthetic admin user (`backend/app/api/deps.py:67-75`).

The emergency kill switch has its own separate secret-header check, not JWT:
`backend/app/api/routes/emergency.py:28`.

### 1.3b HTTP routes — dashboard / demo streaming (`:8010`)

App built in `backend/demo_streaming/api.py:123`. All handlers `async def`.

| Method | Path | Handler | Auth |
|---|---|---|---|
| GET | `/health` | `api.py:134` | none |
| GET | `/demo/positions` | `api.py:148` | `_get_authenticated_user_from_request` (`:43`) |
| GET | `/demo/closed-positions` | `api.py:177` | same |
| GET | `/demo/signals` | `api.py:204` | same |
| GET | `/demo/market-data-health` | `api.py:236` | none |
| GET | `/demo/stream` (SSE) | `api.py:247` | same |
| GET/POST/PATCH/PUT/DELETE | `/api/v1/{full_path:path}` | `api.py:303` | **none at this layer** — proxied upstream, see §3.1a |
| GET | `/`, `/accounts`, `/settings`, `/system-monitor`, `/account/{path}` | `api.py:336-341` | none (SPA shell) |
| GET | `/favicon.svg` | `api.py:344` | none |

`/assets` is a `StaticFiles` mount (`api.py:132`), registered only when the frontend
`dist/assets` directory exists.

### 1.4 Background workers and schedulers (trading process only)

All of these are `asyncio` tasks inside the `:8001` event loop, started from the lifespan
context manager at `backend/app/main.py:35`. None is a separate OS process.

| Worker | Started | Cadence | Notes |
|---|---|---|---|
| `ExecutionWorkerPool` — 10 worker tasks | `backend/app/main.py:159`, started `:165`; tasks created `backend/app/services/worker_pool.py:96` | polls every 0.5 s when idle (`worker_pool.py:71`, `:160`) | the signal consumers |
| Stale-job reclaimer | `backend/app/services/worker_pool.py:100` | every 15 s (`worker_pool.py:69`) | requeues/quarantines expired leases, reconciles orphaned claims (`worker_pool.py:123-152`) |
| Per-job lease heartbeat | `backend/app/services/worker_pool.py:190` | every `max(2.0, lease/3)` ≈ 10 s (`worker_pool.py:233`) | one task per in-flight job |
| `PositionReconciler` | `backend/app/main.py:171`, started `:177`; loop `backend/app/services/position_reconciler.py:272` | every 30 s (`position_reconciler.py:27`) | broker snapshot vs ledger |
| `MarginScanner` | `backend/app/main.py:142`, background start `:169`; loop `backend/app/services/margin_scanner.py:80` | every `margin_rate_refresh_sec` = 300 s (`margin_scanner.py:91`) | disabled by default (`margin_scan_enabled=False`) |
| `CriticalRecoveryService` | `backend/app/main.py:94`; task spawned `backend/app/services/critical_recovery.py:116` | on-demand, retry after 30 s (`critical_recovery.py:27`), max 2 attempts (`:28`) | per-basket, not a polling loop |
| `AccountMarginService` | `backend/app/main.py:119` | one `reqAccountSummary`, then IBKR pushes (`backend/app/services/account_margin.py:196-197`) | not a loop |
| `LivePnlService` | `backend/app/main.py:79` | tick-driven, persist throttled to 1/s per trade (`backend/app/services/pnl.py:37`) | callbacks arrive on the TWS thread |

One-shot startup steps, in lifespan order: hydrate runtime from DB
(`backend/app/main.py:85`) → wire critical recovery (`:94`) → TWS connect (`:105`) →
hydrate live P&L (`:115`) → `RecoveryManager.run_startup_recovery` (`:138`) → startup
margin scan (`:151`) → worker pool (`:165`) → reconciler (`:177`) → enqueue critical
baskets (`:182`).

### 1.4a Background loops in the watchdog process

Separate OS process (`backend/scripts/watchdog_main.py`), separate event loop. Two tasks,
both created in `WatchdogDaemon.start` (`backend/app/services/watchdog/daemon.py:561`).

| Loop | File:line | Cadence |
|---|---|---|
| Main supervision loop — services, safety gates, resources | `daemon.py:539`, task at `:564`, `while` at `:546` | `watchdog_interval_seconds` = 10.0 s (`watchdog/config.py:16`) |
| Command loop | `daemon.py:435`, task at `:567`, `while` at `:439` | consumes queued operator commands |
| Status reporter | `watchdog/status.py:50` | its own `while True` |

Per-pass work: `_check_services` (`daemon.py:527`) → `_check_one` per service (`:156`),
`_check_safety_gates` (`:484`), `_check_resources` (`:383`, throttled to
`resource_check_interval_seconds` = 30.0 s, `config.py:62`).

The watchdog **writes no database tables.** Its only durable state is a JSON file at
`/home/tradingapp/storage/state/watchdog_recovery.json` (`watchdog/config.py:77`, managed by
`watchdog/recovery_store.py`), holding the restart budget.

### 1.5 Schedulers outside the application

- `deploy/systemd/trading-session-start.timer:5` — `OnCalendar=Mon..Fri 09:30:00`
- `deploy/systemd/trading-session-stop.timer:5` — `OnCalendar=Mon..Fri 16:00:00`
- `deploy/systemd/trading-backend-restart.path:5` — restarts the backend when
  `/home/tradingapp/storage/state/restart_backend.trigger` is modified
- `deploy/systemd/demo-streaming-restart.path:5` — same pattern for the dashboard
- `scripts/process_manager.py` enforces the session window in-process:
  `SESSION_START = dtime(9, 30)` and `SESSION_END = dtime(16, 0)` in
  `America/New_York` (`scripts/process_manager.py:94-96`), supervising on a 5 s poll
  (`:113`).

### 1.6 CLI commands

`scripts/process_manager.py` takes group arguments (`webhook`, `gateway`, `fastapi`)
validated against `VALID_GROUPS` at `scripts/process_manager.py:98`; argparse imported at
`:46`.

Under `backend/scripts/` (all standalone, none imported by the running services):
`watchdog_main.py`, `load_test_mft_burst.py`, `prune_webhook_captures.py`,
`repair_historical_killswitch_positions.py`, `test_tws_connection.py`,
`test_tws_market_data.py`, `instrument_master/{discover,discover_cfd,seed_fetcher,seed_paper_cfd,pacer}.py`,
`oms/{flatten_gateway_positions,run_paper_execution}.py`, `rms/run_demo.py`.

`backend/scratch/` holds five ad-hoc inspection scripts
(`check_ib_gateway_connection.py`, `count_allocations.py`, `inspect_db_state.py`,
`inspect_target_signal.py`, `verify_live_market_data.py`).

Shell entrypoints: `scripts/ngrok-control.sh`, `scripts/backend-ready-trigger.sh`,
`scripts/ibgateway-wrapper.sh`, `scripts/webhook-ingest-wrapper.sh`.

---

## 2. Data model

20 tables. Model files are in `backend/app/db/models/`; migrations in
`backend/alembic/versions/`. Four migrations write *data*, not just schema, so Alembic is
itself a writer of `allocations`
(`alembic/versions/h2i3j4k5l6m7_allocation_pair_max_allocation_pct.py:30`,
`b2d8f4a1c903_allocation_max_open_positions.py:27`), `execution_settings`
(`c8e1a4b7d205_execution_settings.py:40`) and `margin_settings`
(`n2o3p4q5r6s7_create_margin_settings_table.py:105`) — backfills that run under
`alembic upgrade head`, outside any of the five runtime processes.

### 2.1 Tables and writers

| Table | Model | Written by |
|---|---|---|
| `signals` | `backend/app/db/models/signal.py:17` | trading (2 paths) |
| `signal_jobs` | `backend/app/db/models/signal.py:66` | **ingest + trading** |
| `execution_claims` | `backend/app/db/models/execution_claim.py:25` | trading |
| `orders` | `backend/app/db/models/order.py:29` | trading |
| `executions` | `backend/app/db/models/execution.py:28` | trading |
| `baskets` | `backend/app/db/models/basket.py:26` | trading |
| `positions` | `backend/app/db/models/position.py:19` | **trading (several writers) + CLI script** |
| `event_log` | `backend/app/db/models/event.py:20` | **trading + CLI script** |
| `broker_positions` | `backend/app/db/models/broker_position.py:29` | trading |
| `position_reconcile_runs` | `backend/app/db/models/broker_position.py:56` | trading |
| `kill_switch_operations` | `backend/app/db/models/kill_switch.py:29` | trading |
| `accounts` | `backend/app/db/models/account.py:14` | trading (config API) |
| `per_symbol_limits` | `backend/app/db/models/account.py:33` | trading (config API) |
| `strategies` | `backend/app/db/models/strategy.py:27` | **no runtime writer** — migrations only |
| `allocations` | `backend/app/db/models/strategy.py:42` | trading (config API) + Alembic backfill |
| `execution_settings` | `backend/app/db/models/execution_settings.py:12` | trading (config API + lazy create) + Alembic backfill |
| `margin_settings` | `backend/app/db/models/margin_settings.py:14` | trading (config API + lazy create) + Alembic backfill |
| `margin_rates` | `backend/app/db/models/margin_rate.py:23` | trading (margin scanner) |
| `instruments` | `backend/app/db/models/instrument.py:14` | trading + CLI seed scripts |
| `users` | `backend/app/db/models/user.py:28` | **no application writer** — see §2.3 |

### 2.2 Tables written by more than one process

**`signal_jobs` — two processes.**
- Ingest inserts: `backend/app/api/routes/webhooks.py:250`
  (`create_job_if_not_exists`), inside the `:8000` app.
- Trading updates: claim (`backend/app/services/worker_pool.py:175`), status writes
  (`worker_pool.py:268`), lease heartbeat (`worker_pool.py:241`), reclaim sweep
  (`worker_pool.py:129`), startup recovery (`backend/app/services/recovery.py:99`, `:117`,
  `:132`).

This is the only cross-*process* shared-write table by design; the split is
insert-by-ingest, update-by-trading.

**`positions` — one process, but five distinct writers inside it.** Worth flagging because
they do not all go through the same code path:
- Model Blue open/close on the signal path: `backend/app/services/model_blue/persistence.py:163`
  and `:213`
- Live P&L tick persistence: `backend/app/services/pnl.py:820` (`update_live_pnl`) — this
  write originates on the **TWS callback thread**, marshalled to the loop at
  `backend/app/services/pnl.py:751`
- Kill switch flatten: `backend/app/services/kill_switch.py:439` and `:495`
- Single-pair close service: `backend/app/services/position_close_service.py:216`
- Alternate trade-book path: `backend/app/services/model_blue/db_trade_book.py:59`, `:72`
- Out-of-band CLI: `backend/scripts/repair_historical_killswitch_positions.py:144`

**`instruments` — trading process plus CLI.** Runtime CFD discovery writes via
`backend/app/instruments/cfd_discover.py` (invoked from
`backend/app/services/order_manager.py:1480` and `:591`); the seeding scripts
`backend/scripts/instrument_master/seed_fetcher.py`, `discover.py`, `discover_cfd.py`,
`seed_paper_cfd.py` write the same table offline.

**`event_log` — trading process plus the same CLI.** The normal writer is
`EventRepository.append`, called from the signal path, kill switch, reconciler and recovery.
`backend/scripts/repair_historical_killswitch_positions.py:152` also appends a
`POSITION_CLOSE` row with `source: "HISTORICAL_REPAIR_SCRIPT"`, using an
`idempotency_key` of `position_close:repair:{account_id}:{trade_id}` — the same script that
writes `positions` at `:144`.

**`strategies` — nothing writes it at runtime.** No `StrategyModel(...)` construction,
`insert`, `update` or `delete` exists outside migrations and tests; the config API reads it
to validate `allocations` rows but never mutates it. Strategy rows arrive via migration or
by hand.

**`demo_streaming` writes nothing.** Every database access in
`backend/demo_streaming/snapshot.py` and `api.py` is a `select` (see `snapshot.py:285`
through `:501`); the module docstring at `backend/demo_streaming/publisher.py:1` states
read-only. It publishes to Redis instead.

### 2.3 `users` has no application write path

`backend/app/api/routes/auth.py` exposes only `/login` (`:49`), `/sse-token` (`:99`) and
`/me` (`:112`) — no registration or user-creation endpoint. A repo-wide search for
`UserModel(` outside tests returns two hits, both transient objects that are never added to
a session: the `TRADINGAPP_TESTING` synthetic admin at `backend/app/api/deps.py:68` and its
counterpart at `backend/demo_streaming/api.py:70`. The migration
`backend/alembic/versions/g1h2i3j4k5l6_create_users_table.py:21` creates the table and
indexes but seeds no rows. Rows must therefore arrive out of band.

### 2.4 Constraints that carry semantics

| Constraint | File:line |
|---|---|
| `signals` unique `(strategy_id, signal_id)` — `uq_signals_strategy_signal` | `backend/app/db/models/signal.py:39` |
| `signal_jobs.idempotency_key` unique | `backend/app/db/models/signal.py:75` |
| `execution_claims.dedupe_key` unique | `backend/app/db/models/execution_claim.py:28` |
| `orders.internal_order_id` unique | `backend/app/db/models/order.py:36` |
| `executions.exec_id` unique — `uq_executions_exec_id` | `backend/app/db/models/execution.py:63` |
| `event_log.idempotency_key` unique | `backend/app/db/models/event.py:37` |
| `baskets` unique `(account_id, trade_id, action)` | `backend/app/db/models/basket.py:55` |
| `broker_positions` unique `(ibkr_account, con_id)` | `backend/app/db/models/broker_position.py:49` |
| `allocations` unique `(account_id, strategy_id)` | `backend/app/db/models/strategy.py:68` |
| `margin_rates` unique `(symbol, instrument_type, side)` | `backend/app/db/models/margin_rate.py:44` |
| `strategies.strategy_id` unique | `backend/app/db/models/strategy.py:30` |
| `users.email` unique | `backend/app/db/models/user.py:31` |
| `positions` composite PK `(account_id, trade_id)` | `backend/app/db/models/position.py:21-24` |

Check constraints: `margin_rates.rate` in (0,1] and side in BUY/SELL
(`backend/app/db/models/margin_rate.py:50-51`); `allocations.alloc_pct` in [0,1] and
`pair_max_allocation_pct` in (0,1] (`backend/app/db/models/strategy.py:71-77`).

---

## 3. I/O boundaries

Every point where the system talks to something outside itself.

### 3.1 Inbound HTTP

| Boundary | File:line |
|---|---|
| TradingView webhook POST | `backend/app/api/routes/webhooks.py:167` |
| Operator/dashboard REST on `:8001` | routers listed in §1.3 |
| Dashboard REST + SSE on `:8010` | `backend/demo_streaming/api.py:123` (app), SSE stream at `:283` |
| Static frontend assets mounted on `:8010` | `backend/demo_streaming/api.py:132` (`StaticFiles`) |

No app in the repo calls `add_middleware`. There is no CORS middleware, no rate-limit
middleware and no WebSocket endpoint anywhere; the only push channel is the SSE stream.

### 3.1a Demo app proxies the trading API

`backend/demo_streaming/api.py:303` registers a catch-all
`@app.api_route("/api/v1/{full_path:path}", methods=["GET","POST","PATCH","PUT","DELETE"])`
whose handler `proxy_trading_api` forwards the request to
`{trading_api_url}/api/v1/...` over `httpx` (`:321`), copying the client's headers through
except `host`, `content-length` and `connection` (`:315`), then returns the upstream body
and status verbatim (`:331`).

Two consequences worth recording as facts:
- This is both an inbound boundary on `:8010` and an **outbound HTTP boundary** to `:8001`
  — process-to-process, not counted in §3.2.
- The proxy route itself carries **no `Depends`** — no `get_current_user`, no
  `require_admin`. Authentication is whatever the upstream trading route enforces on the
  forwarded `Authorization` header. Every mutating route in §1.3, including
  `/config/accounts/{id}/square-off` and the `service-control` systemd endpoints, is
  reachable at `:8010` under the same path.

### 3.2 IBKR — outbound

All outbound IBKR traffic goes through the single `TWSClient`
(`backend/app/broker/ibkr/tws_client.py:21`).

| Call | File:line |
|---|---|
| TCP connect + reader thread start | `backend/app/broker/ibkr/tws_client.py:546`, thread at `:552` |
| `placeOrder` (real orders) | `backend/app/oms/ibkr_adapter.py:438` |
| `placeOrder` (what-if margin probe, `whatIf=True`) | `backend/app/oms/ibkr_adapter.py:367` |
| `cancelOrder` | `backend/app/oms/ibkr_adapter.py:514`, and post-probe at `:377` |
| `reqOpenOrders` / `reqExecutions` | `backend/app/oms/ibkr_adapter.py:470`, `:477` |
| `reqContractDetails` (blocking, off-loop via `to_thread`) | `backend/app/broker/ibkr/tws_client.py:428`; async wrapper `:449` |
| `reqPositions` / `cancelPositions` | `backend/app/broker/ibkr/tws_client.py:474`, `:470`, `:480`; async wrapper `:499` |
| `reqAccountSummary` | `backend/app/services/account_margin.py:313` |
| `reqMktData` / `cancelMktData` | `backend/app/services/pnl.py:685` (`_issue_request_ticks`), cancel at `:131` |
| `disconnect` | `backend/app/broker/ibkr/tws_client.py:579` |

### 3.3 IBKR — inbound (all land on the TWS reader thread)

`TWSClient` subclasses `EWrapper` and fans callbacks out to registered listeners.

| Callback | TWSClient | Adapter handler |
|---|---|---|
| `nextValidId` | `tws_client.py:76` | — (sets handshake event) |
| `managedAccounts` | `tws_client.py:89` | consumed at `ibkr_adapter.py:139` |
| `error` | `tws_client.py:103` | `ibkr_adapter.py:947` |
| `connectionClosed` | `tws_client.py:151` | `ibkr_adapter.py:1017` |
| `orderStatus` | `tws_client.py:300` | `ibkr_adapter.py:627` |
| `openOrder` | `tws_client.py:287` | `ibkr_adapter.py:731` |
| `execDetails` | `tws_client.py:359` | `ibkr_adapter.py:786` |
| `commissionReport` | `tws_client.py:381` | `ibkr_adapter.py:889` |
| `position` / `positionEnd` | `tws_client.py:263`, `:276` | `backend/app/broker/ibkr/positions.py` collector |
| `accountSummary` | `tws_client.py:239` | `backend/app/services/account_margin.py:235` |
| `tickPrice` / `tickSize` | `tws_client.py:171`, `:180` | `backend/app/services/pnl.py:350`, `:403` |
| `contractDetails` | `tws_client.py:209` | used by CFD discovery |
| `rerouteMktDataReq` | `tws_client.py:198` | `backend/app/services/pnl.py:416` |

### 3.4 PostgreSQL

Single async engine, created at `backend/app/db/session.py:41` from
`create_engine_from_settings` (`:18`); session factory `AsyncSessionLocal` at `:44`.
Production pool: `pool_size=20`, `max_overflow=30`, `pool_timeout=30`, `pool_recycle=1800`
(`backend/app/db/session.py:32-36`). Under `TRADINGAPP_TESTING=1` it swaps to `NullPool`
(`:24-28`). The demo process builds its own engine
(`backend/demo_streaming/main.py:13`).

Migrations are applied out-of-band by Alembic (`backend/alembic/env.py`).

### 3.5 Redis

Only in the dashboard process. Client created at `backend/demo_streaming/main.py:36`;
`XADD` at `backend/demo_streaming/stream.py:34`; `XREAD` at `:41`; `PING` at `:26`.
Default URL `redis://127.0.0.1:6379/0` (`backend/demo_streaming/config.py:15`).

### 3.6 Filesystem

| What | File:line |
|---|---|
| Raw webhook JSON capture dir | `backend/app/api/routes/webhooks.py:26`, written at `:63` |
| Append-only accepted-signal CSV (marked TEMPORARY) | `backend/app/api/routes/webhooks.py:31`, written at `:127`, called off-loop via `asyncio.to_thread` at `:283` |
| Dated log directories | `backend/app/core/logger.py`; supervisor at `scripts/process_manager.py:69` |
| systemd restart trigger files | `scripts/process_manager.py:70-71` |
| Frontend `dist/assets` served | `backend/demo_streaming/api.py:132` |

### 3.7 Other network egress

- Telegram Bot API from the watchdog: `backend/app/services/watchdog/telegram.py` (HTTP via
  `httpx`, retry/backoff at `:90`, `:97`).
- Watchdog health probes over HTTP against the running services:
  `backend/app/services/watchdog/health.py:273`, `:323`, `:410`, `:497`; raw TCP probes at
  `:155`, `:599`, `:679`; its own Postgres engine at `:618`; Redis ping at `:701`.
- Watchdog safety actions calling back into the trading API:
  `backend/app/services/watchdog/safety.py:44`, `:75`, `:100`, `:117`, `:155`.
- `SystemMonitorService` making HTTP calls to sibling services:
  `backend/app/services/system_monitor_service.py:334`, `:393`, `:450`, `:499`, `:524`, `:555`.
- Demo dashboard proxying `/api/v1/*` to the trading API over `httpx`:
  `backend/demo_streaming/api.py:321` (see §3.1a).
- Subprocess spawning (IB Gateway, Xvfb, uvicorn) from the supervisor:
  `scripts/process_manager.py:218`.
- **`systemctl` invoked from an HTTP route.** `backend/app/api/routes/service_control.py`
  (router prefix `/service-control`, `:17`) shells out via `subprocess` (`:7`) against a
  fixed allowlist of four units (`:20-26`) and four actions (`:29`), admin-gated by
  `require_admin`. This is the trading process reaching out to the host init system.

---

## 4. The signal path

One TradingView alert, end to end. **Bold** hops cross a process or thread boundary.

### Hop 1 — HTTP receipt (ingest process, `:8000`)

`receive_tradingview_webhook` — `backend/app/api/routes/webhooks.py:167` (async).
Auth check first at `:176` → `_verify_webhook_authentication` (`:144`), constant-time
compare at `:154`.

### Hop 2 — Validation

Body read `:178`, JSON parse `:182` (400 on failure `:188`), dict-shape check `:193`.
This is the *only* validation in the ingest process — the payload is not schema-validated
against a strategy here.

### Hop 3 — Idempotency key

`compute_idempotency_key` — `backend/app/services/worker_pool.py:28`, called at
`backend/app/api/routes/webhooks.py:232`. Normalises strategy id (`:35`) and trade id
(`:37`), appends `:CLOSE` for close actions (`:41-42`), SHA-256 of
`strategy:signal:action` (`:46-47`).

### Hop 4 — Persistence to the queue

`SignalJobRepository.create_job_if_not_exists` — called at
`backend/app/api/routes/webhooks.py:250`, defined at
`backend/app/db/repositories/signal_repository.py:302`, with the insert at `:332` using
`ON CONFLICT DO NOTHING` on `idempotency_key` (`:335`). Duplicate returns the existing job
and `created=False` (`:342-344`); the HTTP response is still `202 accepted`
(`backend/app/api/routes/webhooks.py:298-304`). Disk capture CSV appended at `:283`.

### **Hop 5 — Process boundary: ingest → trading**

The handoff is the `signal_jobs` table. No queue broker, no HTTP call, no shared memory.
Ingest returns 202 and forgets. The trading process discovers the row by polling.

### Hop 6 — Worker claim (trading process, `:8001`)

`_worker_loop` — `backend/app/services/worker_pool.py:154` → `_claim_job` (`:171`) →
`SignalJobRepository.claim_next_jobs` (`backend/app/db/repositories/signal_repository.py:347`).
Uses `SELECT ... FOR UPDATE SKIP LOCKED` (`:393`), ordered by `received_at` (`:392`), and
skips any job whose `trade_id` has a sibling holding a live lease (`:366-375`, `:387-390`)
so OPEN cannot be handed out concurrently with its CLOSE.

Then `_process_claimed_job` (`:179`) takes the in-process domain lock keyed on
`(account_scope, strategy_id)` (`:181`, `:195`), starts the lease heartbeat task (`:190`),
and re-checks lease ownership after acquiring the lock (`:199`).

### Hop 7 — Payload parse into a domain Signal

`_execute_job` (`backend/app/services/worker_pool.py:287`) writes status `PROCESSING`
(`:302`) then calls `OrderManager.parse_inbound_payload`
(`backend/app/services/order_manager.py:629`) → `parse_tradingview_payload`
(`backend/app/services/strategies/inbound.py:17`). Strategy lookup at `:28`; falls back to
`parse_legacy_signal` (`:51`) when no handler matches. Parse failure ⇒ job `REJECTED`
(`worker_pool.py:319`).

### Hop 8 — Pipeline entry and inbound persistence

`OrderManager.process_signal_execution` — `backend/app/services/order_manager.py:656`,
invoked from `worker_pool.py:325`. Writes the inbound `signals` row at
`order_manager.py:668` (`_persist_inbound_signal`, `:1368`) plus two `event_log` rows
(`:1391`, `:1403`).

### Hop 9 — Account fan-out

`_process_signal_execution_inner` (`order_manager.py:692`) resolves eligible accounts via
`DatabaseStrategyAccountRouter.resolve` (`backend/app/accounts/router.py:63`), which joins
`accounts × allocations × strategies` with all three `enabled` flags true
(`router.py:75-81`). No eligible accounts ⇒ raise (`order_manager.py:719`).

`_fanout_accounts` (`order_manager.py:789`) runs one coroutine per account with
`asyncio.gather` (`:803`). **These run concurrently in the same event loop** — not a thread
or process boundary, but a concurrency boundary that matters for the shared in-memory RMS
state.

### Hop 10 — Pre-RMS gates (per account)

`_fanout_single_account` — `order_manager.py:727`:
- margin free-funds gate on OPEN: `:745` → `_assert_account_has_free_margin` (`:408`)
- intent construction by the strategy handler: `:746`
- kill-switch gate: `:748` → `is_account_kill_switch_active`
  (`backend/app/services/kill_switch.py:60`), reading a process-local cache

### Hop 11 — Exposure lock, then RMS

`_evaluate_and_submit` (`order_manager.py:1007`) wraps everything in `_exposure_guard`
(`:972`), which acquires per-`(account, symbol)` locks in sorted order (`:995-1001`).

`_evaluate_and_submit_locked` (`order_manager.py:1021`): zero-quantity guard (`:1031`),
then `RMSEngine.evaluate` (`backend/app/rms/engine.py:54`) called at `order_manager.py:1038`.

Checks run in fixed order (`backend/app/rms/engine.py:32-39`): MARGIN (1), DUPLICATE (2),
STRATEGY (3), CONTRACT MONTH (4), OPEN-POSITION LIMIT (7), MONEY PER STOCK (8),
MODEL MARKET VALUE (101). Short-circuits on REJECT/HALT (`engine.py:97`). Result is
audited to `event_log` at `order_manager.py:1039` → `_audit_rms` (`:1136`). Non-PASS raises
(`:1043`).

### Hop 12 — Instrument resolution

`_resolve_instruments` — `order_manager.py:1458`, called at `:1046`. Discovers/upserts CFD
`conId`s via `ensure_cfd_instruments_for_symbols` (`:1480`), which issues
`reqContractDetails` against IBKR. Attaches resolved contracts (`:1492`) and re-checks
quantity after size-increment rounding (`:1496`). Audited to `event_log` at `:1501`.

Then margin metadata annotation (`:1049`) and the borderline what-if confirmation
(`:1050` → `_confirm_margin_if_borderline`, `:468`), which issues a real
`placeOrder(whatIf=True)` to IBKR at `ibkr_adapter.py:367`.

### Hop 13 — Basket-critical gate

`order_manager.py:1057-1071` — blocks new OPENs when
`BasketCoordinator.is_open_blocked(account_id, strategy_id)`
(`backend/app/oms/coordinator.py:88`) is true.

### Hop 14 — Execution claim (the durable dedupe barrier)

`_acquire_execution_claim` — `order_manager.py:902`, called at `:1077`, immediately before
anything can reach the broker. Commits in its own transaction (`:912`) so the barrier is
visible independent of the surrounding work.
`ExecutionClaimRepository.acquire` — `backend/app/db/repositories/execution_claim_repository.py:41`:
insert-or-retake in one statement, with the `ON CONFLICT` arm restricted to previously
`ABANDONED` rows (`:82`). Raises `DuplicateExecutionError` (`:95`),
`ExecutionInFlightError` (`:102`), or `ClaimNeedsReconciliationError` (`:107`).

### Hop 15 — Gateway selection

**There is none.** `BasketCoordinator` is constructed with the single `OMSService`
(`order_manager.py:183`), which holds the single adapter
(`backend/app/oms/oms_service.py:28`), which holds the single `TWSClient`
(`backend/app/oms/ibkr_adapter.py:82`). Account targeting happens by setting
`ib_order.account` on the order object at `backend/app/oms/ibkr_adapter.py:240-242`, and
the gateway is validated against `managedAccounts` at `ibkr_adapter.py:144`.

The nearest thing to selection is pacing priority:
`_order_priority` (`ibkr_adapter.py:244`) returns `PRIORITY_EMERGENCY_FLATTEN` for flatten
intents, else `PRIORITY_ORDER_EXECUTION`.

### Hop 16 — Basket submit

`BasketCoordinator.execute` — `backend/app/oms/coordinator.py:195`, called from
`order_manager.py:1081`. Persists the basket row (`:219`), emits `BASKET_CREATED` /
`BASKET_EXECUTING` events (`:220`, `:241`), then submits legs sequentially in a loop
(`:263`) via `OMSService.submit_one_leg` (`backend/app/oms/oms_service.py:162`), persisting
each child order (`coordinator.py:275`).

### Hop 17 — Rate limiter, then IB submit

`OMSService._submit_leg` (`oms_service.py:201`) builds the `OMSOrder` (`:251`) and calls
`IBKRExecutionAdapter.submit_order` (`ibkr_adapter.py:384`) at `oms_service.py:283`.

Inside `submit_order`: connection check (`:386`), **rate-limiter acquire**
(`:391` → `_acquire_for_order`, `:250` → `GatewayRateLimiter.acquire`,
`backend/app/broker/ibkr/gateway_rate_limiter.py:136`), managed-account validation (`:393`),
duplicate-internal-id guard (`:397`), TWS order-id reservation (`:403` →
`_get_next_tws_order_id`, `:201`), contract/order build (`:406-407`), map registration
before the call so callbacks cannot race (`:411-414`), then **`placeOrder`** at `:438`.

### **Hop 18 — Thread boundary: IB callbacks**

IBKR replies arrive on the `TWSClientThread` daemon thread started at
`backend/app/broker/ibkr/tws_client.py:552`, **not** the asyncio loop.

- `orderStatus` → `ibkr_adapter.py:627`; mutates order state under `self._lock` (`:643`).
- `execDetails` → `ibkr_adapter.py:786`; dedupes on `execId` (`:810-811`), builds a
  `BrokerExecution` fill record (`:843`), updates weighted-average price (`:863`).
- `commissionReport` → `ibkr_adapter.py:889`; attaches commission to the exec record
  (`:931`), buffers when the exec has not arrived yet (`:929`).
- `error` → `ibkr_adapter.py:947`; feeds Error 100 back to the limiter (`:950`).

Two mechanisms cross back to the event loop:
1. **Future resolution** — `_notify_future_if_terminal` (`ibkr_adapter.py:613`) uses
   `loop.call_soon_threadsafe` (`:625`) to wake the coroutine parked in
   `wait_for_terminal_or_fill` (`:517`), which the coordinator awaits at
   `coordinator.py:609`.
2. **Persistence scheduling** — `_emit_order_state` (`ibkr_adapter.py:123`) invokes
   `BasketCoordinator._on_broker_order_state` (`coordinator.py:1135`) **on the TWS thread**,
   which hands off with `asyncio.run_coroutine_threadsafe` (`coordinator.py:1150`).

### Hop 19 — Fill booking

`_persist_broker_snapshot` (`coordinator.py:1170`) → `_persist_child` (`:1019`), which
writes the `orders` row (`OrderRepository.record_oms_order`, `:1037`) and then upserts every
`BrokerExecution` into the `executions` ledger (`ExecutionRepository.upsert`, `:1048`).
Event rows emitted at `:1187` with per-kind idempotency keys from `_event_idempotency_key`
(`:1156`).

Basket completeness is judged on cumulative fill quantity per leg
(`_basket_complete`, `coordinator.py:586`). Complete ⇒ state `OPEN` or `CLOSED` (`:366-371`).
Incomplete ⇒ retry (`:352`), then cancel + compensate (`:407`, `:427`), then `CRITICAL`
(`:963`).

### Hop 20 — Position booking

Back in `order_manager.py:1100`: on `OPEN`/`CLOSED` the claim is sealed (`:1101`), fills are
folded into the intent (`:1102` → `_intent_with_fills`, `:1175`), and
`_update_runtime_state` runs (`:1103`).

`_update_runtime_state` (`:1273`) calls `handler.after_submit` (`:1320`) →
`ModelBlueStrategy.after_submit` (`backend/app/services/model_blue/strategy.py:100`) →
`ModelBlueExecutionPersistence.persist_open` (`backend/app/services/model_blue/persistence.py:146`)
or `persist_close` (`:203`). `persist_open` rebuilds the pair row from actual filled
quantities and weighted-average fill prices (`_open_trade_from_fills`, `:60`) and refuses to
persist unless both legs are FILLED (`:67`, `:73`).

### Hop 21 — Exposure update

In-memory only, in `_update_runtime_state` (`order_manager.py:1273`), still under the
exposure guard from hop 11:
- `processed_signals` (the RMS duplicate set) — `:1283`
- `open_positions` counter — `:1285`
- `symbol_exposures` per `(account, symbol)` — `:1290`
- `model_value_used` — `:1296`
- margin commitments — `:1300` (`_commit_margin`, `:391`)

CLOSE decrements the same structures (`:1301-1319`).

A separate path books exposure for baskets that never settled:
`_record_unsettled_exposure` (`order_manager.py:1210`), called at `:1115`.

### Hop 22 — P&L

Two distinct mechanisms.

**Realised (on close, from the fill ledger):**
`PositionRepository.close_trade` (`backend/app/db/repositories/position_repository.py:193`)
computes `signed_qty * (exit_mark - entry_mark)` per leg (`:207-218`) and subtracts
commission (`:220-223`). Exit marks come from
`_exit_marks_from_orders` (`backend/app/services/model_blue/persistence.py:29`), which
prefers the weighted average over the `executions` records (`:33`).

**Unrealised (live, from market data — not the fill ledger):**
`LivePnlService` subscribes to ticks (`backend/app/services/pnl.py:511`). Ticks land on the
**TWS thread** at `pnl.py:350`, `_recompute` runs there (`:728`), pair P&L computed at
`:740-743`, then **`asyncio.run_coroutine_threadsafe`** at `:751` marshals to the loop for
`_schedule_persist` (`:761`), which throttles to one write per second per trade
(`:37`, `:778-781`) and finally writes `positions.live_pnl` via
`PositionRepository.update_live_pnl` (`pnl.py:820`).

### Hop 23 — Job terminal status

Back in the worker: `worker_pool.py:327` checks whether the lease was lost mid-flight (and
if so deliberately writes nothing, `:335`), then writes `RECOVERY_REQUIRED` (`:344`),
`REJECTED` (`:356`), `FAILED` (`:373`), or `COMPLETED` (`:384`). All go through
`_write_status` (`:257`), which fences on `worker_id` (`:273`).

### Boundary summary

| Boundary | Where |
|---|---|
| Process: ingest → trading | `signal_jobs` table; write `webhooks.py:250`, read `signal_repository.py:347` |
| Thread: loop → TWS socket | `placeOrder` at `ibkr_adapter.py:438` from the loop; `EClient` writes on the caller's thread |
| Thread: TWS reader → loop (wake) | `loop.call_soon_threadsafe`, `ibkr_adapter.py:625` |
| Thread: TWS reader → loop (persist) | `asyncio.run_coroutine_threadsafe`, `coordinator.py:1150` |
| Thread: TWS reader → loop (P&L) | `asyncio.run_coroutine_threadsafe`, `pnl.py:751` |
| Thread: loop → blocking IB call | `asyncio.to_thread`, `tws_client.py:449` and `:499` |
| Thread: loop → disk | `asyncio.to_thread`, `webhooks.py:283` |
| Concurrency: per-account fan-out | `asyncio.gather`, `order_manager.py:803` |
| Concurrency: 10 workers | `worker_pool.py:96` |

---

## 5. Guard inventory

### 5.1 Database-level

| Guard | Protects | File:line |
|---|---|---|
| `FOR UPDATE SKIP LOCKED` on job claim | two workers claiming the same job | `backend/app/db/repositories/signal_repository.py:393` |
| Sibling-`trade_id` exclusion in the claim query | OPEN and CLOSE on one trade running concurrently | `backend/app/db/repositories/signal_repository.py:366-375`, `:387-390` |
| `signal_jobs.idempotency_key` unique + `ON CONFLICT DO NOTHING` | duplicate webhook delivery | `backend/app/db/models/signal.py:75`; `signal_repository.py:335` |
| `execution_claims.dedupe_key` unique | the same intent reaching the broker twice, across workers *and* across process restarts | `backend/app/db/models/execution_claim.py:28`; acquire `execution_claim_repository.py:41` |
| Claim `ON CONFLICT` arm limited to `ABANDONED` | retaking a live or already-executed claim | `execution_claim_repository.py:82` |
| Worker-id fenced status write | a worker writing terminal status after losing its lease | `signal_repository.py:452-457`; caller `worker_pool.py:257` |
| Worker-id fenced heartbeat | renewing a lease you no longer own | `signal_repository.py:474-481` |
| `event_log.idempotency_key` unique | duplicate audit rows on retry | `backend/app/db/models/event.py:37` |
| `executions.exec_id` unique | double-booking a broker fill | `backend/app/db/models/execution.py:63` |
| `orders.internal_order_id` unique | duplicate order ledger rows | `backend/app/db/models/order.py:36` |
| `signals` unique `(strategy_id, signal_id)` + `on_conflict_do_nothing` | duplicate signal FK rows from the OMS path | `backend/app/db/models/signal.py:39`; used `coordinator.py:1121` |
| `baskets` unique `(account_id, trade_id, action)` | duplicate basket rows | `backend/app/db/models/basket.py:55` |
| `broker_positions` unique `(ibkr_account, con_id)` | duplicate snapshot lines | `backend/app/db/models/broker_position.py:49` |

**No Postgres advisory locks exist anywhere in the codebase** — a repo-wide search for
`pg_advisory` / `pg_try_advisory` returns nothing.

### 5.2 Lease / TTL

| Guard | Value | File:line |
|---|---|---|
| Job lease duration | 30 s | `backend/app/services/worker_pool.py:60` |
| Lease heartbeat interval | `max(2.0, lease/3)` ≈ 10 s | `backend/app/services/worker_pool.py:233` |
| Reclaimer sweep interval | 15 s | `backend/app/services/worker_pool.py:69` |
| Claim staleness threshold | 300 s | `backend/app/services/worker_pool.py:61`; repo default `execution_claim_repository.py:50` |
| Max job attempts before dead-letter | 3 | `backend/app/db/repositories/signal_repository.py:486` |
| Lease-expiry disposition | `CLAIMED` → requeue; `PROCESSING` → quarantine as `RECOVERY_REQUIRED` | `signal_repository.py:513-527`, `:529-542` |
| Startup claim reconciliation (`stale_after_sec=0`) | all claims held by the dead process | `backend/app/services/recovery.py:79` |

### 5.3 In-process locks (single process, not distributed)

| Lock | Protects | File:line |
|---|---|---|
| Domain lock, keyed `(account_scope, strategy_id)` | serialises jobs for one strategy across the 10 workers | `worker_pool.py:75`, `:83`, held `:195` |
| Guard lock for the domain-lock dict | dict mutation | `worker_pool.py:76` |
| Exposure lock, keyed `(account, symbol)` / `__margin__` / `__model_value__` | the RMS read-modify-write on `symbol_exposures`, which spans strategies and so is not covered by the domain lock — reasoning at `order_manager.py:974-986`; sorted acquisition to avoid deadlock `:995` | `order_manager.py:169`, `:963`, `:972` |
| Adapter `threading.Lock` | all order maps and futures shared with the TWS thread | `ibkr_adapter.py:92` |
| Rate limiter `threading.Lock` | token buckets touched from both loop and TWS threads | `gateway_rate_limiter.py:84` |
| `PositionReconciler._sweep_lock` | overlapping reconcile sweeps | `position_reconciler.py:266`, checked `:307` |
| P&L `_persist_lock` | tick-driven persist bookkeeping across threads | `pnl.py:104` |
| Kill-switch semaphore (concurrency limit) | concurrent position flattens | `kill_switch.py:154` |
| TWS registry / contract-details / positions locks | request-id maps and snapshot collection | `tws_client.py:41`, `:43`, `:48` |
| CSV write lock | the temporary incoming-signals CSV | `webhooks.py:32` |
| `AccountMarginService` lock | snapshot dict written from the TWS thread | `account_margin.py:182` |

### 5.4 In-memory dedup sets (process-local, lost on restart)

| Guard | File:line |
|---|---|
| `RMSContext.processed_signals` — RMS check 2 | `backend/app/rms/checks/duplicate.py:30`; rehydrated at `order_manager.py:198` |
| `OMSService._submitted_signals` — duplicate intent per `(account, signal_id)` | `oms_service.py:31`, checked `:78` |
| Adapter duplicate internal-order-id check | `ibkr_adapter.py:397-401` |
| `_seen_exec_ids` — execution dedupe | `ibkr_adapter.py:108`, checked `:810` |
| `_commissioned_exec_ids` — commission dedupe | `ibkr_adapter.py:109`, checked `:916` |
| `_broker_acked` / `_fill_event_emitted` / `_partial_qty_emitted` — event emission dedupe | `ibkr_adapter.py:106`, `:107`, `:111`; logic `:696`, `:703` |
| `BasketCoordinator._retry_ids` — duplicate retry suppression | `coordinator.py:78`, checked `:728` |
| `BasketCoordinator._critical` — OPEN latch per `(account, strategy)` | `coordinator.py:79`, checked `:88`, rehydrated `:185` |
| Kill-switch blocked-account cache | `kill_switch.py:60`, hydrated `:75`, called from `order_manager.py:229` |

The docstring at `backend/app/db/models/execution_claim.py:1-8` states explicitly that the
in-memory sets cannot survive a crash and that `execution_claims` is the authoritative
barrier.

### 5.5 Pacing

`GatewayRateLimiter` (`backend/app/broker/ibkr/gateway_rate_limiter.py:44`) is a
dual-bucket token limiter: a global bucket and a narrower "normal" bucket, so priority-0
emergency flatten traffic can spend the reserve slice that P1–P4 cannot
(`_try_consume_locked`, `:259`). Three entry points: async `acquire` (`:136`, raises
`GatewayPacingTimeout`), non-blocking `try_acquire` (`:117`), and `blocking_acquire` for
TWS/worker threads (`:192`). Error 100 drains both buckets and starts a cooldown
(`notify_error_100`, `:102`), triggered from `ibkr_adapter.py:950` and `tws_client.py:130`.

---

## 6. Config surface

### 6.1 Where configuration comes from

1. **Environment / `.env`** → `Settings` (`backend/app/core/config.py:12`), loaded via
   `get_settings()` (`:135`). `env_file=".env"`, `extra="ignore"`
   (`:20-25`) — unknown env keys are silently dropped.
2. **Database tables, read at runtime** — `execution_settings`, `margin_settings`,
   `margin_rates`, `accounts`, `allocations`, `strategies`, `per_symbol_limits`. Reloaded by
   `OrderManager.reload_execution_policy` (`order_manager.py:272`),
   `reload_margin_settings` (`:300`), `reload_margin_rates` (`:321`), `reload_rms_limits`
   (`:258`). Mutated through `/api/v1/config/*` (`backend/app/api/routes/config.py:46`).
3. **Demo dashboard settings** — a separate `BaseSettings` at
   `backend/demo_streaming/config.py`.
4. **systemd unit files** — `deploy/systemd/*`, each with
   `EnvironmentFile=/home/tradingapp/app/backend/.env`.
5. **Supervisor module constants** — `scripts/process_manager.py:67-133`, some overridable
   by env, most not.
6. **Watchdog config** — `backend/app/services/watchdog/config.py`.

A guard rail: `get_settings()` refuses to run against the production database when
`TRADINGAPP_TESTING=1` (`backend/app/core/config.py:142-148`).

`backend/.env.example` documents keys that no longer exist in `Settings` and are therefore
ignored — `BROKER_MODE` (`.env.example:10`) and `ALLOCATIONS_CONFIG_PATH`
(`.env.example:58`). It also omits `DATABASE_URL`, `JWT_SECRET_KEY`, `WEBHOOK_AUTH_SECRET`
and `EMERGENCY_KILLSWITCH_AUTH_SECRET`, which do exist
(`config.py:33`, `:38`, `:110`, `:114`).

### 6.2 Operationally significant defaults in `Settings`

All in `backend/app/core/config.py`.

| Setting | Default | Line |
|---|---|---|
| `database_url` | `postgresql+asyncpg://root:root123@localhost:5433/ibkr_trading` | 33 |
| `jwt_secret_key` | hardcoded placeholder string | 38 |
| `jwt_access_token_expire_minutes` | 480 | 42 |
| `ibkr_port` | 7497 | 46 |
| `ibkr_client_id` | 1 | 47 |
| `ibkr_connection_timeout` | 10 | 48 |
| `ibkr_gateway_max_msg_per_sec` | 30.0 | 51 |
| `ibkr_gateway_normal_msg_per_sec` | 24.0 | 52 |
| `ibkr_gateway_emergency_reserve_per_sec` | 6.0 | 53 |
| `ibkr_gateway_max_wait_sec` | 8.0 | 54 |
| `ibkr_gateway_error100_cooldown_sec` | 2.0 | 55 |
| `min_order_notional` | 100 | 58 |
| `pair_ratio_tolerance` | 0.5 | 59 |
| `market_value_utilisation_cap` | 1.0 | 64 |
| `market_value_check_enabled` | **False** | 65 |
| `margin_whatif_enabled` | **False** | 68 |
| `margin_whatif_timeout_sec` | 5.0 | 69 |
| `margin_scan_enabled` | **False** | 72 |
| `margin_scan_max_per_sec` | 5.0 | 73 |
| `margin_scan_startup_budget_sec` | 20.0 | 74 |
| `margin_rate_max_age_days` | 7 | 79 |
| `margin_rate_refresh_sec` | 300 | 80 |
| `margin_snapshot_max_age_sec` | 300 | 81 |
| `ibkr_market_data_type` | 3 (delayed) | 84 |
| `order_quantity` | 1 | 95 |
| `model_blue_committed_notional` | `None` | 102 |
| `paper_execute_stk_as_cfd` | **True** | 107 |
| `webhook_auth_enabled` | True, secret `None` | 110-111 |
| `emergency_killswitch_auth_enabled` | True, secret `None` | 113-114 |

### 6.3 Hardcoded numeric constants (not configurable)

**Concurrency and leases** — `backend/app/main.py:162` worker_count=10;
`worker_pool.py:60` lease 30 s, `:61` reclaim 15 s, `:62` claim-stale 300 s,
`:63` idle poll 0.5 s, `:233` heartbeat divisor 3; `signal_repository.py:486`
max_attempts=3.

**Basket timing** — `order_manager.py:186` `fill_timeout=90.0`, `:187`
`cancel_timeout=30.0`. `fill_timeout` is later overwritten from `execution_settings`
(`coordinator.py:172`); `cancel_timeout` is not.

**Rate limiter** — `gateway_rate_limiter.py:15` documented IBKR ceiling 50/s;
`:17-21` defaults 30 / 24 / 6 / 8 s wait / 2 s cooldown; `:23-27` priority levels 0–4.

**Reconciliation / recovery** — `position_reconciler.py:27` 30 s interval, `:28` 15 s
positions timeout, `:29` `QTY_EPSILON=1e-6`; `critical_recovery.py:26` 15 s, `:27` 30 s
retry, `:28` `MAX_RECOVERY_ATTEMPTS=2`.

**Fill arithmetic** — `coordinator.py:48` `_FILL_EPS=1e-8`;
`critical_recovery.py:29` same; `ibkr_adapter.py:38` `_MAX_SANE_PRICE=1e12`.

**P&L** — `pnl.py:37` persist throttle 1.0 s; `pnl.py:21-29` IBKR tick-type IDs;
`pnl.py:590` contract-details timeout 3.0 s; `pnl.py:118` paced-retry delay 0.05 s.

**Position limits, defaulted in code** — `order_manager.py:149` `max_open_positions=100`,
`:150` `money_limit_per_symbol=10_000_000`; repeated at `:1448`. Both are per-strategy
fallbacks used when no DB row exists.

**Contract month** — `order_manager.py:91` `_STK_CONTRACT_MONTH = "2026-09"`, hardcoded on
the legacy single-name path (`:891`).

**IBKR error-code policy** — `ibkr_adapter.py:943` non-terminal warnings `{399, 2109}`;
`:945` rejection codes `{200, 201, 10147, 10148, 10243}`; special-cased 202 at `:978`, 100
at `:970`.

**DB pool** — `db/session.py:33-36` pool_size 20, max_overflow 30, timeout 30,
recycle 1800.

**Account margin** — `account_margin.py:143` `max_age_sec=300`; `:48` request-id base 70000.

**Watchdog** — `backend/app/services/watchdog/config.py:16` poll interval 10.0 s; `:20`
`recovery_max_attempts=5` within `:21` `recovery_window_seconds=600`; `:22` verify timeout
30.0 s, `:23` verify poll 2.0 s; `:26` notification cooldown 300.0 s; `:28`
`escalation_interval_seconds=0.0` (escalation off); `:62` resource check 30.0 s. These are
Pydantic-settings fields with defaults, so they are overridable by environment, unlike the
supervisor constants below.

**Supervisor** — `scripts/process_manager.py:75-77` host `127.0.0.1`, ports 8000/8001;
`:81` health timeout 2.0 s; `:90` expected gateway restart 03:52 ET, `:91` ±12 min window;
`:94-96` session window 09:30–16:00 ET; `:113` poll 5 s; `:116-117` max 5 restarts per 600 s;
`:120` shutdown grace 15 s; `:123` Xvfb settle 2 s; `:127` gateway API port **4002**;
`:130` gateway ready timeout 180 s; `:131` ready poll 1.0 s; `:133` API settle 5.0 s.
Note `4002` here versus `ibkr_port=7497` in `Settings`.

**Demo dashboard** — `demo_streaming/config.py:17` port 8010, `:18` poll 2000 ms,
`:20` P&L emit 5000 ms.

**Watchdog timeouts** — `watchdog/daemon.py:163` 5.0 s, `:441` 10 s, `:480` 2.0 s;
`watchdog/health.py:155` 1.0 s, `:273` 3.5 s, `:410` 2.0 s, `:618` pool_timeout 2.0 s,
`:701` 1.5 s; `watchdog/safety.py:44` 3.0 s; `watchdog/telegram.py:90` backoff `min(2**n, 10)`,
`:97` `min(2**n, 5)`.

**System monitor** — `system_monitor_service.py:334`, `:393`, `:450`, `:499`, `:524`, `:555`
all use a 1.5 s HTTP timeout.

---

## 7. Test coverage sketch

### 7.1 How the suite runs

`backend/tests/conftest.py` forces `TRADINGAPP_TESTING=1` at import time (`:14`) and
rewrites `DATABASE_URL` to a **real Postgres database** named `ibkr_trading_test`
(`:18`, `:21`, `:31`). A session-scoped autouse fixture creates that database if missing
and runs the full Alembic migration chain against it (`:38`, `:59`, `:76-78`). So the
schema and its constraints are genuinely exercised — this is not SQLite and not mocked.

Other fixtures: `session_factory` (`conftest.py:84`), webhook capture-dir redirect
(`:98`), kill-switch cache reset between tests (`:108`).

Two behavioural flags are forced off for the whole suite at import time:
`WEBHOOK_AUTH_ENABLED=false` (`conftest.py:16`) and `PAPER_EXECUTE_STK_AS_CFD=false`
(`:13`). Tests that need auth re-enable it per-test (`test_webhook_authentication.py:22`).

IBKR is faked, never contacted. `backend/tests/ibkr_test_utils.py:1` states so; the fake
works by patching `placeOrder` to synthesise fills — `fill_on_place_order`
(`ibkr_test_utils.py:32`) — plus `wire_test_managed_accounts` (`:18`) to satisfy the
managed-accounts gate.

### 7.2 Coverage by signal-path hop

| Hop | § | Covered? | Tests |
|---|---|---|---|
| HTTP receipt | 4.1 | yes | `test_tradingview_webhook.py:34`, `:65`, `:85`, `:103`; `test_webhook_ingest.py:62` (asserts the trading app has no webhook route) |
| Webhook auth | 4.1 | yes, thoroughly | `test_webhook_authentication.py:48`, `:72`, `:97`, `:136`, `:160`, `:207` |
| JSON validation | 4.2 | yes | `test_tradingview_webhook.py:65`, `:85`; `test_webhook_authentication.py:287` |
| Idempotency key | 4.3 | yes | `test_mft_concurrency_recovery.py:27` |
| Durable enqueue | 4.4 | yes | `test_mft_concurrency_recovery.py:47`; `test_webhook_authentication.py:260`, `:239` (DB-failure → 500) |
| **Process boundary** | 4.5 | **not traversed** | `test_webhook_authentication.py:301`, `test_webhook_ingest.py:54` assert the two apps stay separate; no test enqueues a job and then lets a worker consume it — see §7.4 |
| Worker claim / SKIP LOCKED | 4.6 | yes | `test_mft_concurrency_recovery.py:80` |
| **Lease heartbeat + stale reclaim** | 4.6 | **no** | no test references `heartbeat_lease` or `reclaim_stale_jobs` |
| Payload parse | 4.7 | yes | `test_n_leg_execution.py`, `test_signal_payload_persistence.py`, `test_multi_account_routing.py` |
| Worker terminal-status classification | 4.23 | yes | `test_worker_recovery_classification.py:42`, `:63`, `:87` |
| Account fan-out | 4.9 | yes | `test_fanout_isolation.py:91`; `test_multi_account_routing.py` |
| RMS engine + ordering | 4.11 | yes | `rms/test_rms_engine.py:18`, `:45`, `:65`, `:92`; per-check files under `tests/rms/` |
| Margin gates / what-if | 4.10, 4.12 | yes | `test_whatif_probe.py:53`, `:86`, `:108`, `:125`, `:142`; `test_margin_gate.py`, `test_margin_band.py`, `test_margin_check.py`, `test_margin_tally.py` |
| Kill-switch gate | 4.10 | yes | `test_kill_switch.py:40`, `:73`, `:116`; `test_emergency_kill_switch.py`; `test_kill_switch_start_again.py`; `test_kill_switch_reconciliation_fix.py` |
| Instrument resolution | 4.12 | yes | `test_instrument_resolution.py:66`–`:226`; `test_stk_to_cfd_demo_override.py`; `test_cfd_discover.py` |
| Basket-critical gate | 4.13 | yes | `test_basket_coordinator.py:261`; `test_critical_baskets_api.py` |
| **Execution claim barrier** | 4.14 | **no** | no test file references `execution_claims`, `ExecutionClaimRepository`, or `dedupe_key` |
| Gateway selection | 4.15 | n/a | feature does not exist |
| Basket submit / compensation / retry | 4.16 | yes, thoroughly | `test_basket_coordinator.py:182`–`:360`; `test_basket_retry.py`; `test_n_leg_execution.py`; `test_naked_pair_protection_fix.py` |
| Rate limiter | 4.17 | yes | `test_gateway_rate_limiter.py:19`, `:34`, `:57`, `:70`, `:84`, `:98`; `test_ibkr_adapter_pacing.py` |
| IB submit | 4.17 | yes | `test_oms.py:132`–`:238`; `test_ibkr_adapter_managed_accounts.py:73`, `:85`, `:97`, `:113` |
| **IB callbacks (thread boundary)** | 4.18 | yes as logic, **not as concurrency** | `test_oms.py:267`, `:303`, `:334`, `:412`, `:433`, `:454`; `test_execution_audit_persistence.py:302`. Callbacks are invoked synchronously from the test thread by the fake, so the real `call_soon_threadsafe` / `run_coroutine_threadsafe` marshalling is not exercised |
| Fill booking / executions ledger | 4.19 | yes | `test_execution_audit_persistence.py:166`, `:232`, `:281`, `:405`; `test_persist_executions_snapshot.py:57` |
| Position booking | 4.20 | yes | `test_db_model_blue_persistence.py`; `test_tradingview_execution_integration.py:197`, `:228`, `:242` |
| Exposure update | 4.21 | yes | `test_risk_ceiling_2000.py`; `test_margin_tally.py`; `rms/test_rms_money_per_stock.py`; `test_mft_concurrency_recovery.py:115`, `:183` |
| Realised P&L from ledger | 4.22 | yes | `test_execution_audit_persistence.py:405` (`test_persisted_executions_reproduce_realized_pnl`) |
| Live P&L from ticks | 4.22 | yes | `test_market_data_pipeline.py:63`–`:351` (12 tests); `test_market_value_helpers.py` |
| Position reconciliation | — | yes | `test_position_reconciler.py:72`–`:237` |
| Crash recovery | — | partial | `CriticalRecoveryService` well covered (`test_critical_recovery.py:24`–`:401`); `RecoveryManager.run_startup_recovery` is referenced only through app-wiring fixtures, not tested directly |

### 7.3 Uncovered hops, stated plainly

1. **`execution_claims` — the durable dedupe barrier.** Described in
   `backend/app/db/models/execution_claim.py:1-8` as the authoritative guard against
   double execution across crashes and workers. No test in `backend/tests/` references it.
   `acquire`, `mark_executed`, `release` and `reconcile_stale_claims`
   (`backend/app/db/repositories/execution_claim_repository.py:41`, `:118`, `:135`, `:169`)
   have no direct coverage, and neither do the three exception types it raises.
2. **Lease heartbeat and stale-lease reclamation.**
   `SignalJobRepository.heartbeat_lease` (`signal_repository.py:464`) and
   `reclaim_stale_jobs` (`:486`) are untested, as is the fenced-write path
   `worker_pool.py:257` and the lease-lost branches at `worker_pool.py:199` and `:327`.
   The three lease-expiry dispositions (requeue / quarantine / dead-letter) are not
   exercised.
3. **Real cross-thread marshalling.** Because the IBKR fake calls back synchronously, no
   test drives `loop.call_soon_threadsafe` (`ibkr_adapter.py:625`) or
   `asyncio.run_coroutine_threadsafe` (`coordinator.py:1150`, `pnl.py:751`) from an actual
   separate thread.
4. **`RecoveryManager.run_startup_recovery`** (`backend/app/services/recovery.py:35`) —
   no direct test. It appears only as a patched `AsyncMock` in lifespan fixtures
   (`test_api.py:36`, `test_app_wiring.py:39`,
   `test_burst_stress_500_and_kill_switch.py:122`), so the code that reconciles
   non-terminal jobs on startup never runs under test.
5. **Individual symbols with no referencing test:** `_worker_loop`
   (`worker_pool.py:155`) — the loop body itself, as opposed to `_execute_job`;
   `OMSService.submit_one_leg`; `_on_broker_order_state` (`ibkr_adapter.py:731`, the
   `openOrder` handler); `_update_runtime_state` (`order_manager.py`). The sibling
   `_record_unsettled_exposure` is covered (`test_order_manager_model_value.py:98`,
   `test_margin_tally.py:116`), so the gap in hop 4.21 is one-sided.
6. **Partial:** `OrderManager._resolve_instruments` is never called directly by a test —
   it is either mocked out (`test_close_single_pair.py:177`,
   `test_kill_switch_reconciliation_fix.py:32`, `test_broker_flatten_api.py:67`) or
   reached transitively through `process_signal_execution`. The lower-level
   `resolve_leg` / `attach_resolved` primitives are directly covered
   (`test_instrument_resolution.py:66`–`:271`).

### 7.4 Integration vs unit

**No test drives the whole path.** The suite is split in two at the process boundary
(§4.5), and neither half crosses it:

- *Ingest-side integration* stops at the `signal_jobs` insert. These tests post real HTTP
  to the `webhook_ingest` app — `test_tradingview_webhook.py:34`,
  `test_webhook_authentication.py:48`–`:326`, `test_burst_stress_150_300.py:53`, `:87`,
  `test_burst_stress_500_and_kill_switch.py:56`, `:89` — but every one of them patches
  `ExecutionWorkerPool.start` to an `AsyncMock`
  (e.g. `test_burst_stress_500_and_kill_switch.py:111`), so the rows they enqueue are
  never claimed or executed.
- *Execution-side integration* starts after the claim. These tests call
  `OrderManager.process_signal_execution` directly against real Postgres and the IBKR fake
  — `test_tradingview_execution_integration.py` (helper `_execute_signal_async` at `:186`,
  used by the tests at `:197`–`:337`), `test_multi_account_routing.py`,
  `test_production_path_hardening.py`, `test_hardening_lifecycle.py`,
  `test_db_model_blue_persistence.py`, `test_execution_audit_persistence.py`,
  `test_basket_coordinator.py`. Its fixture also stubs the worker pool
  (`test_tradingview_execution_integration.py:159`), and hops 4.1–4.6 — HTTP receipt,
  auth, idempotency key, enqueue, claim — are skipped entirely.

So `signal_jobs` is written by one set of tests and read by none; the handoff that couples
the two processes is covered only by `test_webhook_execution_separation`
(`test_webhook_authentication.py:301`), which asserts the *absence* of execution rather
than the presence of a consumer.

Everything else is unit or component level.

### 7.5 Skipped tests

Five, all in one file, all with the same reason string
*"Database persistence in webhook route temporarily paused for execution engine
integration task"*:
`backend/tests/test_tradingview_signal_persistence.py:27`, `:33`, `:39`, `:56`, `:62`.
No `xfail` markers anywhere in the suite.

---

## 8. Unclear

The codebase is heavily docstringed — nearly every module states its purpose in line 1.
The list below is short as a result, and I have kept it to things I genuinely could not
resolve from the code rather than padding it.

1. **`backend/app/services/model_blue/trade_book.py` vs `db_trade_book.py`.** Both exist and
   both implement the trade-book protocol. `main.py:73` injects the DB-backed one, and
   `ModelBlueStrategy` falls back to `InMemoryModelBlueTradeBook` when none is passed
   (`backend/app/services/model_blue/strategy.py:62`). Whether the in-memory version is
   still a supported runtime configuration or exists only for tests is not stated anywhere.
   The same ambiguity applies to `allocation.py`'s
   `TemporarySettingsCommittedCapitalProvider` (`strategy.py:168`, `:180`) versus
   `db_allocation.py`'s `DatabaseCommittedCapitalProvider` (`main.py:72`).

2. **`backend/app/instruments/paper_cfd_catalog.py`.** A hardcoded table of IBKR paper CFD
   `conId`s. Only importer is `backend/scripts/instrument_master/seed_paper_cfd.py`. Whether
   it is a live seed source or a stale snapshot is not determinable from the code.

3. **`backend/scratch/`** — five scripts (`check_ib_gateway_connection.py`,
   `count_allocations.py`, `inspect_db_state.py`, `inspect_target_signal.py`,
   `verify_live_market_data.py`). No docstrings tying them to a workflow, no importers, not
   referenced by any doc. They read as one-off debugging aids but I cannot confirm that.

4. **`_STK_CONTRACT_MONTH = "2026-09"`** (`backend/app/services/order_manager.py:91`). Used
   only on the legacy single-name path (`:891`) to satisfy RMS check 4. Why equities carry
   a contract month at all, and what happens after that date, is not explained in the code.

5. **Port disagreement between the supervisor and the app.**
   `scripts/process_manager.py:127` sets `GATEWAY_API_PORT = 4002` (IB Gateway paper) and
   waits for the gateway on it, while `Settings.ibkr_port` defaults to `7497` (paper TWS)
   at `backend/app/core/config.py:46`. The runtime `.env` presumably reconciles these, but
   which is authoritative is not stated in code.

6. **How `users` rows are created.** Confirmed in §2.3 that nothing in the application
   inserts them and no migration seeds them, so they must be created out of band — but by
   what (a manual SQL step, an undocumented ops script, a fixture) is not recorded anywhere
   in the repo.

7. **`backend/app/services/watchdog/state_machine.py` authority.** The watchdog can restart
   services (`backend/app/services/watchdog/safety.py`) and the systemd `.path` units also
   restart the backend on a trigger file (`deploy/systemd/trading-backend-restart.path:5`),
   and `scripts/process_manager.py` supervises the same children. Which of the three is the
   intended owner of restart decisions is not stated anywhere I could find.
