# Backend execution path (debug orders)

**Verified from:** `backend/app/api/routes/webhooks.py`, `backend/app/webhook_ingest.py`, `backend/app/services/worker_pool.py`, `backend/app/services/order_manager.py`, `backend/app/services/recovery.py`, `backend/app/services/strategies/inbound.py`, `backend/app/accounts/router.py`, `backend/app/services/model_blue/`, `backend/app/rms/engine.py`, `backend/app/oms/coordinator.py`, `backend/app/oms/ibkr_adapter.py`, `backend/app/main.py`, `backend/app/core/logger.py`.

Use this file when a signal is missing, rejected, or incompletely filled. For queue/lease details see [`backend-concurrency.md`](backend-concurrency.md). Root [`AGENTS.md`](../AGENTS.md) points here.

## Process shape

Two FastAPI processes:

| Process | Module | Port | Role |
|---------|--------|------|------|
| Webhook ingest | `app.webhook_ingest:app` | 8000 | Auth, validate, enqueue `signal_jobs` (Postgres only) |
| Trading / execution | `app.main:app` | 8001 | TWS → OMS → OrderManager → workers |

Trading lifespan wires:

`TWSClient` → `IBKRExecutionAdapter` (+ `GatewayRateLimiter`) → `OMSService` → `OrderManager` → `RecoveryManager` → `ExecutionWorkerPool(10)` on `app.state`.

There is **no** separate Strategy / per-account OMS / Risk process. Ingest is split from execution; strategy/OMS/risk still run in-process on the trading app.

**IB connectivity as-is (PARTIAL):** exactly **one** `TWSClient` socket (`Settings.ibkr_host` / `ibkr_port` / `ibkr_client_id`). Multi-account does **not** mean multi-Gateway. See [`backend-multi-gateway.md`](backend-multi-gateway.md).

## Live path (ingest → queue → worker → execute)

```
POST /api/webhooks/tradingview  (ingest :8000, HTTP 202)
  → receive_tradingview_webhook
  → write capture JSON under backend/data/tradingview_webhooks/
  → compute_idempotency_key (normalize strategy/trade; CLOSE → signal_id:trade_id:CLOSE)
  → SignalJobRepository.create_job_if_not_exists → signal_jobs (QUEUED)
  → return status=accepted, job_id

ExecutionWorkerPool on trading app :8001 (background)
  → claim_next_jobs (lease + same-trade_id serialization)
  → domain lock (account_scope, strategy_id)
  → OrderManager.process_signal_execution
       → persist SignalModel (status NEW)
       → DatabaseStrategyAccountRouter.resolve(strategy_id)
       → asyncio.gather per AccountExecutionContext  (in-process; still one job)
            → kill-switch gate on OPEN
            → ModelBlueStrategy.build_intent (sizer uses ctx.committed_notional)
            → exposure_guard → RMSEngine.evaluate (checks 2, 3, 4, 7, 8)
            → instrument resolve (catalog / paper STK→CFD; auto CFD conId discovery)
            → execution_claims.acquire (durable dedupe barrier, account-scoped key)
            → BasketCoordinator.execute
                 → OMSService.submit_one_leg → IBKRExecutionAdapter.submit_order
                    (ib_order.account = intent.ibkr_account; same socket for every account)
                 → TWSClient → the single IB Gateway / TWS
            → seal claim EXECUTED on settled basket
            → ModelBlueExecutionPersistence + position / trade-book updates
            → LivePnlService.watch_open / unwatch
       → update signal_jobs → COMPLETED | REJECTED | FAILED
```

**Legacy inline path:** synchronous `process_signal_execution` in the webhook handler only when `worker_pool is None` **and** `session_factory is None` (tests).

## Webhook HTTP outcomes

From `webhooks.py` (HTTP **202 Accepted** on success):

| `status` field | Meaning |
|----------------|---------|
| `accepted` | Payload valid; job enqueued (or duplicate idempotency key) |
| `rejected` | Invalid payload (`ValueError` at parse) |
| `rejected_by_rms` | Legacy inline path only — RMS rejection |

RMS/OMS rejection on the normal path sets **`signal_jobs.status = REJECTED`**, not a different HTTP status. HTTP 202 `accepted` does **not** mean filled.

Malformed JSON → HTTP 400.

## Job outcomes (check Postgres `signal_jobs`)

| `status` | Meaning |
|----------|---------|
| `QUEUED` / `CLAIMED` / `PROCESSING` | In progress |
| `COMPLETED` | Pipeline finished successfully |
| `REJECTED` | Parse or RMS/OMS policy rejection |
| `FAILED` | Execution incomplete or exception |
| `RECOVERY_REQUIRED` | Quarantined — orders may exist |
| `DEAD_LETTER` | Max attempts exceeded |

## What runs at startup

From `main.py` lifespan:

1. `setup_logging(level=settings.log_level)` → daily file `storage/logs/{YYYY-MM-DD}/trading.log` + stderr.
2. Build adapter / OMS / OrderManager (+ DB capital, trade book, persistence, LivePnl).
3. `order_manager.hydrate_runtime_from_db()` — RMS context, open positions, kill-switch cache, critical baskets, execution policy.
4. `client.connect_and_start(...)` to `ibkr_host:ibkr_port` (the **only** IB session).
5. On successful connect: `hydrate_live_pnl()`. On failure: lifespan logs that the adapter will “auto-reconnect on active traffic.” **`IBKRExecutionAdapter.submit_order` does not reconnect** — it raises `ConnectionError` if `not is_connected()`. `on_connection_closed` marks in-memory working orders `ERROR` without `reqOpenOrders`.
6. Store `session_factory`, `client`, `ibkr_adapter`, `oms`, `order_manager` on `app.state`.
7. `RecoveryManager.run_startup_recovery()` — reconcile stale jobs/claims.
8. `ExecutionWorkerPool(worker_count=10).start()` → `app.state.worker_pool`.

Shutdown: stop worker pool, disconnect TWS.

Orders listed by `GET /api/v1/orders` come from the **in-memory** OMS map, not a SQL query.

## Log file and useful greps

Logger: `backend/app/core/logger.py` → `/home/tradingapp/storage/logs/{YYYY-MM-DD}/trading.log` (midnight rollover).

Format: `%(asctime)s | %(levelname)-8s | %(name)s | %(trace)s | %(message)s` where `trace` carries `req=` / `signal=` / `trade=` / `acct=` from ContextVars.

| Grep | Stage |
|------|--------|
| `Webhook HTTP 202 accepted` | Job enqueued |
| `Duplicate webhook received for idempotency_key` | Idempotent replay |
| `Rejected invalid TradingView payload` | Parse failure |
| `ExecutionWorkerPool started with` | Workers ready |
| `Worker .* starting execution for job_id` | Worker executing |
| `Worker .* LOST its lease` | Lease fencing |
| `Stale lease sweep` / `Orphaned claim sweep` | Reclaimer |
| `Inbound parse:` / `Model Blue parse` | Strategy handler |
| `KILL_SWITCH_ACTIVE: Blocking NEW open signal` | Kill switch OPEN block |
| `Model Blue size_open` / `OPEN intent` / `CLOSE intent` | Sizing |
| `RMS check` / `RMS evaluate` | Per-check RMS trail |
| `Acquired execution claim` | Pre-broker dedupe barrier |
| `BASKET_CRITICAL gate blocked` | CRITICAL open block |
| `Basket fill wait complete` | Per-leg completeness |
| `Startup recovery scan found` | Recovery scanner |
| `Active execution pipeline: IBKRExecutionAdapter` | Lifespan ready |
| `Initial TWS connection attempt unconfirmed` | Connect failed at startup |
| `BASKET_CREATED` / `BASKET_UNWINDING` / `BASKET_COMPENSATED` / `BASKET_CRITICAL` | Basket coordinator |
| `POSITION_OPEN persisted` / `POSITION_CLOSE persisted` | Model Blue persistence |
| `CFD discover upserted` / `CFD discover:` | Auto CFD conId discovery |
| `LivePnl reqMktData` | PnL market-data subscribe |
| `IBKR submit paced` | GatewayRateLimiter delay |
| `Gateway pacing timeout` | Limiter max_wait exceeded — no placeOrder |
| `IBKR Error 100 cooldown` | IB throttle backoff on limiter |
| `TWS placeOrder failed` | Adapter place failure |
| `EMERGENCY KILL SWITCH ACTIVATED` | Kill switch started |

## Account resolution per order (as-is)

1. Router: `DatabaseStrategyAccountRouter.resolve(strategy_id)` — enabled account × allocation rows (`accounts/router.py`).
2. Intent: `ModelBlueStrategy._build_open_intent` / `_build_close_intent` set `OrderIntent.account_id` and `ibkr_account`.
3. Broker: `IBKRExecutionAdapter._build_ibkr_order` copies `ib_order.account`. IBKR routes the order to that account **on the connected Gateway login**. There is no second `TWSClient`.

One webhook → one `signal_jobs` row (`account_scope` left `NULL`) → worker → N-account fan-out. See [`backend-concurrency.md`](backend-concurrency.md).

## Not on this path

- No automatic target / stop / time_limit exit loop (columns may exist on allocations; no exit-trigger process).
- Demo SSE UI (`demo_streaming`) is a **separate** process; it does not place orders.
- Production pacing is `GatewayRateLimiter` (~30 msg/sec global, 24 normal, 6 emergency reserve for P0 flatten) on the **single** adapter (all accounts, including kill-switch). Covers `placeOrder`, `cancelOrder`, and `reqMktData` (P3 try_acquire).
- No N-Gateway pool, no per-gateway limiter, no reconnect/failover — [`backend-multi-gateway.md`](backend-multi-gateway.md) (target, not as-is).

## Related docs

- Queue/leases/claims: [`backend-concurrency.md`](backend-concurrency.md)
- Kill switch: [`backend-kill-switch.md`](backend-kill-switch.md)
- RMS/basket: [`backend-rms-oms.md`](backend-rms-oms.md)
- Multi-gateway target: [`backend-multi-gateway.md`](backend-multi-gateway.md)
