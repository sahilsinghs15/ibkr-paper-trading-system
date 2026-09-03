# Backend persistence

**Verified from:** `backend/app/db/models/*`, `backend/app/db/repositories/*`, `backend/alembic/versions/*`, `backend/app/db/session.py`, `backend/demo_streaming/*`, grep for `redis` under `backend/app/`.

## Postgres tables (ORM)

| Table | Model | File |
|-------|-------|------|
| `signals` | `SignalModel` | `db/models/signal.py` |
| `signal_jobs` | `SignalJobModel` | `db/models/signal.py` (not exported in `__init__.py`) |
| `accounts` | `AccountModel` | `db/models/account.py` | `id`, `name`, `ibkr_account`, `total_margin`, `enabled`. **No** gateway host/port/clientId. `total_margin` is an operator-entered **market-value budget** (trading capital), not IBKR margin available. |
| `strategies` | `StrategyModel` | `db/models/strategy.py` |
| `allocations` | `AllocationModel` | `db/models/strategy.py` | Includes `pair_max_allocation_pct` (`Numeric(9,6)`, `(0, 1]`, default 0.10) — fraction of the model allocation used as one pair's market-value budget. |
| `per_symbol_limits` | `PerSymbolLimitModel` | `db/models/account.py` |
| `orders` | `OrderModel` | `db/models/order.py` |
| `positions` | `PositionModel` | `db/models/position.py` |
| `event_log` | `EventLogModel` | `db/models/event.py` |
| `instruments` | `InstrumentModel` | `db/models/instrument.py` |
| `baskets` | `BasketModel` | `db/models/basket.py` |
| `executions` | `ExecutionModel` | `db/models/execution.py` |
| `execution_settings` | `ExecutionSettingsModel` | `db/models/execution_settings.py` |
| `margin_rates` | `MarginRateModel` | `db/models/margin_rate.py` | Directional `(symbol, instrument_type, side)` what-if rates |
| `margin_settings` | `MarginSettingsModel` | `db/models/margin_settings.py` | Singleton (`id = 1`) operator margin-gate policy |
| `execution_claims` | `ExecutionClaimModel` | `db/models/execution_claim.py` |
| `kill_switch_operations` | `KillSwitchOperationModel` | `db/models/kill_switch.py` |
| `broker_positions` | `BrokerPositionModel` | `db/models/broker_position.py` | Latest IBKR inventory snapshot (full replace each sweep) |
| `position_reconcile_runs` | `PositionReconcileRunModel` | `db/models/broker_position.py` | One row per reconcile sweep + mismatch summary |

There is **no** `signal_legs` table. Legs live in signal payload / pair columns on `signals` (and related persistence helpers), not a child table.

There are **no** `gateways`, `gateway_clients`, or `account_gateway_bindings` tables. Multi-gateway mapping is target-only ([`backend-multi-gateway.md`](backend-multi-gateway.md)).

## Alembic revisions (HEAD `k5l6m7n8o9p0`)

| Revision | File | Topic |
|----------|------|-------|
| `d4bd73bb4fde` | `d4bd73bb4fde_initial_foundation.py` | No-op foundation |
| `af6ded376ee5` | `af6ded376ee5_create_persistent_schema.py` | Initial tables: signals, accounts, strategies, allocations, per_symbol_limits, orders, event_log, positions, instruments |
| `c3e9f1a2b4d6` | `c3e9f1a2b4d6_add_trade_id_and_closed_at.py` | trade_id / closed_at / internal_order_id |
| `a8f3c1d2e4b5` | `a8f3c1d2e4b5_account_strategy_routing.py` | allocations.enabled; positions PK |
| `b7c4e8a1d902` | `b7c4e8a1d902_basket_atomicity.py` | baskets table |
| `e8a2c4d6f901` | `e8a2c4d6f901_position_instrument_types.py` | leg instrument types |
| `f1b3c5d7e902` | `f1b3c5d7e902_instrument_size_increment.py` | size_increment |
| `a9c4e6f8b013` | `a9c4e6f8b013_executions_and_fill_precision.py` | executions + fill precision |
| `b2d8f4a1c903` | `b2d8f4a1c903_allocation_max_open_positions.py` | max_open_positions |
| `c8e1a4b7d205` | `c8e1a4b7d205_execution_settings.py` | execution_settings singleton |
| `c9a1b2c3d4e5` | `c9a1b2c3d4e5_create_signal_jobs.py` | signal_jobs durable queue |
| `d1e2f3a4b5c6` | `d1e2f3a4b5c6_create_kill_switch_operations.py` | kill_switch_operations |
| `e2f4a6c8d105` | `e2f4a6c8d105_create_execution_claims.py` | execution_claims dedupe barrier |
| `f3a5b7d9e206` | `f3a5b7d9e206_signal_jobs_trade_id_status_index.py` | index on trade_id + status |
| `a4c7e2f10938` | `a4c7e2f10938_normalize_strategy_id_keys.py` | backfill normalized strategy_id keys |
| `b6d8f0a2c147` | `b6d8f0a2c147_kill_switch_clear_columns.py` | cleared_at / cleared_by on kill switch |
| `e9f2a7b4c610` | `e9f2a7b4c610_account_default_symbol_limit.py` | accounts.default_symbol_limit |
| `f4a8c2d1e903` | `f4a8c2d1e903_broker_positions_reconcile.py` | broker_positions + position_reconcile_runs |
| `a1b2c3d4e567` | `a1b2c3d4e567_basket_critical_recovery.py` | BASKET_CRITICAL recovery columns |
| `g1h2i3j4k5l6` | `g1h2i3j4k5l6_create_users_table.py` | users |
| `m1n2o3p4q5r6` | `m1n2o3p4q5r6_create_margin_rates_table.py` | margin_rates unique (symbol, type, side) |
| `n2o3p4q5r6s7` | `n2o3p4q5r6s7_create_margin_settings_table.py` | margin_settings singleton |
| `h2i3j4k5l6m7` | `h2i3j4k5l6m7_allocation_pair_max_allocation_pct.py` | allocations.pair_max_allocation_pct |
| `i3j4k5l6m7n8` | `i3j4k5l6m7n8_uppercase_position_and_limit_symbols.py` | uppercase `positions` / `per_symbol_limits` symbols |
| `j4k5l6m7n8o9` | `j4k5l6m7n8o9_unique_armed_kill_switch.py` | partial unique armed kill-switch per account |
| `k5l6m7n8o9p0` | `k5l6m7n8o9p0_margin_check_enabled_default.py` | `margin_settings.check_enabled` default true |

`users` and `strategies` rows are one-off INSERTs (no create-user / create-strategy HTTP API).

## Repositories

Under `backend/app/db/repositories/`:

| Repository | Key methods |
|------------|-------------|
| `SignalRepository` | `get_by_strategy_signal`, `is_processed`, `list_processed_open_keys`, `record_inbound`, `record_processed`, `record_rejected_payload` |
| `SignalJobRepository` | `create_job_if_not_exists`, `claim_next_jobs`, `update_status`, `heartbeat_lease`, `reclaim_stale_jobs`, `count_orders_emitted` |
| `ExecutionClaimRepository` | `acquire`, `mark_executed`, `release`, `count_orders_emitted`, `reconcile_stale_claims` |
| `OrderRepository` | `get_by_internal_id`, `list_by_trade_id`, `record_oms_order` |
| `PositionRepository` | `list_open`, `open_trade`, `close_trade`, `update_live_pnl`, `get_open_by_trade_id` |
| `TradeRepository` | `get_open`, `open_trade`, `close_trade` |
| `EventRepository` | `append` |
| `ExecutionRepository` | `list_by_internal_order_id`, `upsert` |
| `BasketRepository` | `get`, `list_incomplete`, `list_critical`, `has_critical`, `upsert` |
| `InstrumentRepository` | `upsert`, `list_all` |
| `DatabaseInstrumentCatalog` | `find_all`, `find_all_async` |
| `AllocationRepository` | `get_account`, `get_enabled_account`, `get_allocation`, `get_committed_notional` |
| `BrokerPositionRepository` | `replace_snapshot`, `insert_run` |

## In-memory vs durable

| Concern | Where |
|---------|-------|
| Active OMS order map / submit dedup | `OMSService` in-memory |
| RMS runtime context (hydrated at startup) | `OrderManager` / `RMSContext` in-memory; symbol limits reload via `reload_rms_limits()`; margin snapshots + commitment tally in-memory; rates/policy reload via `reload_margin_rates()` / `reload_margin_settings()`; `model_value_used` re-seeded on each reconcile sweep via `after_reconcile_sweep()` |
| Basket CRITICAL set / live basket objects | `BasketCoordinator` in-memory + `baskets` table |
| Kill-switch armed accounts | `_KILL_SWITCH_ACTIVE_ACCOUNTS` in-memory; **authoritative** in `kill_switch_operations` |
| Worker domain locks / exposure locks | `ExecutionWorkerPool` / `OrderManager` in-memory |
| Live marks / PnL subscriptions | `LivePnlService` in-memory; coalesced writes to `positions.live_pnl` (not one Postgres commit per IBKR tick) |
| IBKR broker position snapshot | `broker_positions` table; refreshed every 30s by `PositionReconciler` |
| Durable execution queue | `signal_jobs` table |
| Durable dedupe barrier | `execution_claims` table |
| Durable ledger | Postgres tables above |
| Webhook raw captures | Files under `backend/data/tradingview_webhooks/` |

`GET /api/v1/orders` reads the **in-memory** OMS map, not `OrderRepository`.

For job/claim semantics see [`backend-concurrency.md`](backend-concurrency.md).

## Redis

- **Main trading package `backend/app/`:** no `redis` imports (verified by search). An in-process `GatewayRateLimiter` is correct **only** while a single process submits to IBKR. Multiple uvicorn workers would each have their own limiter (over-budget). Target limiter policy: [`backend-multi-gateway.md`](backend-multi-gateway.md).
- **`demo_streaming/`:** Redis Streams for SSE (`demo_stream_name`, default `positions:stream`). Postgres is polled by `PositionBridge`; Redis fans out to `/demo/stream`. Stream entries are capped with approximate `MAXLEN` (`demo_stream_maxlen`, default `10000`).
