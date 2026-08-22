# Backend persistence

**Verified from:** `backend/app/db/models/*`, `backend/app/db/repositories/*`, `backend/alembic/versions/*`, `backend/app/db/session.py`, `backend/demo_streaming/*`, grep for `redis` under `backend/app/`.

## Postgres tables (ORM)

| Table | Model | File |
|-------|-------|------|
| `signals` | `SignalModel` | `db/models/signal.py` |
| `signal_jobs` | `SignalJobModel` | `db/models/signal.py` (not exported in `__init__.py`) |
| `accounts` | `AccountModel` | `db/models/account.py` |
| `strategies` | `StrategyModel` | `db/models/strategy.py` |
| `allocations` | `AllocationModel` | `db/models/strategy.py` |
| `per_symbol_limits` | `PerSymbolLimitModel` | `db/models/account.py` |
| `orders` | `OrderModel` | `db/models/order.py` |
| `positions` | `PositionModel` | `db/models/position.py` |
| `event_log` | `EventLogModel` | `db/models/event.py` |
| `instruments` | `InstrumentModel` | `db/models/instrument.py` |
| `baskets` | `BasketModel` | `db/models/basket.py` |
| `executions` | `ExecutionModel` | `db/models/execution.py` |
| `execution_settings` | `ExecutionSettingsModel` | `db/models/execution_settings.py` |
| `execution_claims` | `ExecutionClaimModel` | `db/models/execution_claim.py` |
| `kill_switch_operations` | `KillSwitchOperationModel` | `db/models/kill_switch.py` |

There is **no** `signal_legs` table. Legs live in signal payload / pair columns on `signals` (and related persistence helpers), not a child table.

## Alembic revisions (16 files, HEAD `b6d8f0a2c147`)

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

## In-memory vs durable

| Concern | Where |
|---------|-------|
| Active OMS order map / submit dedup | `OMSService` in-memory |
| RMS runtime context (hydrated at startup) | `OrderManager` / `RMSContext` in-memory; symbol limits reload via `reload_rms_limits()` |
| Basket CRITICAL set / live basket objects | `BasketCoordinator` in-memory + `baskets` table |
| Kill-switch armed accounts | `_KILL_SWITCH_ACTIVE_ACCOUNTS` in-memory; **authoritative** in `kill_switch_operations` |
| Worker domain locks / exposure locks | `ExecutionWorkerPool` / `OrderManager` in-memory |
| Live marks / PnL subscriptions | `LivePnlService` in-memory; writes `positions.live_pnl` |
| Durable execution queue | `signal_jobs` table |
| Durable dedupe barrier | `execution_claims` table |
| Durable ledger | Postgres tables above |
| Webhook raw captures | Files under `backend/data/tradingview_webhooks/` |

`GET /api/v1/orders` reads the **in-memory** OMS map, not `OrderRepository`.

For job/claim semantics see [`backend-concurrency.md`](backend-concurrency.md).

## Redis

- **Main trading package `backend/app/`:** no `redis` imports (verified by search).
- **`demo_streaming/`:** Redis Streams for SSE (`demo_stream_name`, default `positions:stream`). Postgres is polled by `PositionBridge`; Redis fans out to `/demo/stream`.
