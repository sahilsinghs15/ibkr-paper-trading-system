# Backend package map

**Verified from:** `backend/app/main.py`, `backend/app/webhook_ingest.py`, `backend/app/**`, `backend/alembic/versions/*`, `backend/pyproject.toml`.

Agent navigation map for `backend/app/`. For execution flow see [`backend-execution.md`](backend-execution.md); for queue/leases see [`backend-concurrency.md`](backend-concurrency.md).

## Directory tree

```
app/
├── main.py                    # Trading FastAPI factory + lifespan (TWS→OMS→OrderManager→recovery→workers), :8001
├── webhook_ingest.py          # Webhook ingest FastAPI (Postgres-only), :8000
├── accounts/                  # Account × strategy routing / config validation
│   ├── config_service.py      # Dashboard/API validation for accounts & allocations
│   ├── context.py             # AccountExecutionContext dataclass
│   └── router.py              # strategy_id → enabled account contexts (NOT an HTTP router)
├── api/
│   ├── deps.py                # get_oms / get_order_manager from app.state
│   ├── router.py              # mounts orders + config under /api/v1
│   └── routes/
│       ├── health.py          # GET /health
│       ├── webhooks.py        # POST /api/webhooks/tradingview (mounted on webhook_ingest only)
│       ├── orders.py          # OMS order list/get/cancel
│       └── config.py          # Account/allocation/limits/execution/kill-switch CRUD
├── broker/ibkr/
│   ├── gateway_rate_limiter.py # Token-bucket GatewayRateLimiter (wired in main)
│   ├── tws_client.py          # IBAPI TWS TCP client wrapper (one instance in lifespan)
│   └── positions.py           # BrokerPositionLine + PositionSnapshotCollector
├── core/
│   ├── config.py              # Settings (pydantic-settings)
│   ├── identifiers.py         # normalize_strategy_id / normalize_trade_id
│   └── logger.py              # Logging setup + request context
├── db/
│   ├── base.py                # SQLAlchemy DeclarativeBase
│   ├── session.py             # Async engine + AsyncSessionLocal
│   ├── models/                # ORM tables (see backend-persistence.md)
│   └── repositories/          # Data access (see backend-persistence.md)
├── instruments/
│   ├── models.py              # InstrumentRecord / resolved contract identity
│   ├── resolver.py            # Signal instrument_type → IBKR contract
│   ├── execution_override.py  # Paper STK→CFD override
│   ├── paper_cfd_catalog.py   # Hardcoded paper CFD master rows
│   └── cfd_discover.py        # reqContractDetails → upsert instruments
├── models/                    # Domain (non-ORM) types
│   ├── signal.py              # Signal / SignalLeg domain
│   └── model_blue_trade.py    # OpenModelBlueTrade domain
├── oms/
│   ├── oms_service.py         # Order lifecycle facade
│   ├── ibkr_adapter.py        # OMS ↔ TWS execution adapter
│   ├── basket.py              # BasketState enum
│   ├── coordinator.py         # Multi-leg basket atomicity
│   ├── models.py              # OMS domain types
│   └── retry_policy.py        # Paper auto square-off / retry knobs
├── rms/
│   ├── engine.py              # Sequential RMS checks
│   ├── models.py              # OrderIntent / RMSContext / etc.
│   └── checks/                # duplicate, strategy, contract_month, position_limit, money_per_stock
├── schemas/
│   ├── api_schemas.py         # OrderSchema (only)
│   ├── config_schemas.py      # Config/kill-switch/execution schemas
│   └── webhook.py             # TradingViewWebhookResponse
└── services/
    ├── order_manager.py       # Signal → RMS → OMS orchestration facade
    ├── worker_pool.py         # Claims signal_jobs; runs execution
    ├── recovery.py            # Startup crash recovery scanner
    ├── kill_switch.py         # Emergency flatten + armed-account cache
    ├── pnl.py                 # Live unrealized P&L via TWS marks
    ├── position_reconciler.py # Periodic IBKR snapshot vs ledger diff (log only)
    ├── model_blue/            # Parse, size, trade book, persistence
    └── strategies/            # Handler registry / inbound parse / legacy
```

Every package above contains runtime code. The former empty placeholders (`app/market_data/`, `app/strategy/`, `app/core/lifecycle.py`) were deleted — do not recreate them as import targets.

## Lifespan order

### `webhook_ingest.py` (ingest, :8000)

1. `setup_logging(..., filename_prefix="webhook")`
2. Set `app.state.session_factory = AsyncSessionLocal`

### `main.py` (trading, :8001)

Startup:

1. `setup_logging(level=settings.log_level)`
2. Build `TWSClient` → `GatewayRateLimiter` → `IBKRExecutionAdapter` → `OMSService` → `OrderManager` (+ DB capital/trade book/persistence; `_live_pnl` attached)
3. `order_manager.hydrate_runtime_from_db()` — RMS context, open positions, kill-switch cache, critical baskets, execution policy
4. `client.connect_and_start(...)` to `ibkr_host:ibkr_port`
5. On successful connect: `hydrate_live_pnl()`
6. Store on `app.state`: `session_factory`, `client`, `ibkr_adapter`, `oms`, `order_manager`
7. `RecoveryManager.run_startup_recovery()` — reconcile stale jobs/claims before workers
8. `ExecutionWorkerPool(worker_count=10).start()` → `app.state.worker_pool`
9. `PositionReconciler(interval=30s).start()` → `app.state.position_reconciler`

Shutdown:

1. `position_reconciler.stop()`
2. `worker_pool.stop()`
3. `client.disconnect_clean()`

## `app.state` attributes

| Attribute | Type | Role |
|-----------|------|------|
| `session_factory` | `AsyncSessionLocal` | DB sessions for routes/repos |
| `client` | `TWSClient` | **The** IBKR TCP transport — not a pool |
| `ibkr_adapter` | `IBKRExecutionAdapter` | placeOrder / cancel / fill callbacks; one pacer |
| `oms` | `OMSService` | In-memory order lifecycle |
| `order_manager` | `OrderManager` | Pipeline facade |
| `worker_pool` | `ExecutionWorkerPool` | Background job consumers |
| `position_reconciler` | `PositionReconciler` | IBKR position snapshot + ledger diff loop |

Kill-switch armed cache is **not** on `app.state`; it lives in `kill_switch.py` module memory and is rebuilt during `hydrate_runtime_from_db()`.

## IB session (as-is)

Lifespan constructs **one** `TWSClient`, **one** `GatewayRateLimiter`, and **one** `IBKRExecutionAdapter` from `IBKR_HOST` / `IBKR_PORT` / `IBKR_CLIENT_ID`. Adding a second unpaced `TWSClient` in a route or worker would bypass the limiter and fight for `client_id`. Target N-Gateway pool: [`backend-multi-gateway.md`](backend-multi-gateway.md).

## Domain vs ORM

| Domain (dataclass) | ORM (SQLAlchemy) | Notes |
|--------------------|------------------|-------|
| `app/models/signal.py` — `Signal` | `app/db/models/signal.py` — `SignalModel` | Inbox audit row |
| — | `SignalJobModel` (same file as SignalModel) | Durable execution queue; **not** exported from `db/models/__init__.py` |
| `app/models/model_blue_trade.py` | `PositionModel` | Trade book / positions |

## Where do I change X?

| Task | File |
|------|------|
| Webhook ingest / enqueue | `api/routes/webhooks.py` |
| Worker pool / job leases | `services/worker_pool.py`, `db/repositories/signal_repository.py` |
| Execution dedupe barrier | `db/repositories/execution_claim_repository.py`, `services/order_manager.py` |
| Startup recovery | `services/recovery.py` |
| Kill switch | `services/kill_switch.py`, `api/routes/config.py` |
| IBKR leftover flatten (operator sidecar, client id 99) | `scripts/oms/flatten_gateway_positions.py` — runbook: [`backend-kill-switch.md`](backend-kill-switch.md) |
| Model Blue parse/size | `services/model_blue/parser.py`, `sizer.py`, `strategy.py` |
| RMS check order / logic | `rms/engine.py`, `rms/checks/*.py` |
| Basket atomicity | `oms/coordinator.py`, `oms/basket.py` |
| IBKR placeOrder | `oms/ibkr_adapter.py`, `broker/ibkr/tws_client.py` |
| Submit pacing (production) | `broker/ibkr/gateway_rate_limiter.py` (wired in `main.py`) |
| Paper retry / square-off knobs | `oms/retry_policy.py`, `db/models/execution_settings.py`, config API |
| Account routing (DB fan-out, not multi-Gateway) | `accounts/router.py` |
| Multi-gateway target (not built) | [`backend-multi-gateway.md`](backend-multi-gateway.md) |
| Instrument STK→CFD | `instruments/execution_override.py`, `resolver.py`, `cfd_discover.py` |
| Config CRUD | `accounts/config_service.py`, `api/routes/config.py` |
| Live PnL marks | `services/pnl.py` |
| IBKR position reconcile | `services/position_reconciler.py`, `broker/ibkr/positions.py`, `services/reconcile_service.py`, `services/broker_flatten_service.py`, `api/routes/reconcile.py` |
| Idempotency / strategy keys | `core/identifiers.py`, `worker_pool.compute_idempotency_key` |

## Alembic HEAD

Chain ends at revision **`f4a8c2d1e903`** (`broker_positions_reconcile.py`). Full chain in [`backend-persistence.md`](backend-persistence.md).

## Ignore / do not treat as source of truth

| Path | Why |
|------|-----|
| `backend/docs/DEVELOPER_EXECUTION_GUIDE.md` | Stale human guide |
| `../Execution_System_Architecture.md` | Target design, not current code |
| `backend/POSTMAN_API_TESTING_GUIDE.md` | Historical; wrong endpoint inventory, references deleted schemas |
| `broker/ibkr/scheduler.py` / `oms/submit_pacer.py` | **Removed** — live pacing is `GatewayRateLimiter` on the adapter |

## Hardcoded constants (not Settings)

| Constant | Location | Value |
|----------|----------|-------|
| Worker count | `main.py` | `10` |
| Job lease duration | `worker_pool.py` | `30s` |
| Reclaim interval | `worker_pool.py` | `15s` |
| Claim stale after | `worker_pool.py` | `300s` |
| Gateway limiter defaults | `core/config.py` | 30/24/6 msg/sec, 8s wait, 2s Error 100 cooldown |
| Kill-switch flatten concurrency | `kill_switch.py` | `5` |
| Position reconcile interval | `position_reconciler.py` | `30s` |
