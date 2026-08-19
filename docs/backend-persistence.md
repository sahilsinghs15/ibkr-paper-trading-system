# Backend persistence

**Verified from:** `backend/app/db/models/*`, `backend/app/db/repositories/*`, `backend/alembic/versions/*`, `backend/app/db/session.py`, `backend/demo_streaming/*`, grep for `redis` under `backend/app/`.

## Postgres tables (ORM)

| Table | Model | File |
|-------|-------|------|
| `signals` | `SignalModel` | `db/models/signal.py` |
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

There is **no** `signal_legs` table. Legs live in signal payload / pair columns on `signals` (and related persistence helpers), not a child table.

## Alembic revisions (9 files under `alembic/versions/`)

| Revision file | Topic (from filename) |
|---------------|------------------------|
| `d4bd73bb4fde_initial_foundation.py` | Initial foundation |
| `af6ded376ee5_create_persistent_schema.py` | Persistent schema |
| `c3e9f1a2b4d6_add_trade_id_and_closed_at.py` | trade_id / closed_at |
| `a8f3c1d2e4b5_account_strategy_routing.py` | Account / strategy routing |
| `b7c4e8a1d902_basket_atomicity.py` | Baskets |
| `e8a2c4d6f901_position_instrument_types.py` | Position instrument types |
| `f1b3c5d7e902_instrument_size_increment.py` | Instrument size increment |
| `a9c4e6f8b013_executions_and_fill_precision.py` | Executions + fill precision |
| `b2d8f4a1c903_allocation_max_open_positions.py` | Per-account `allocations.max_open_positions` |

## Repositories

Under `backend/app/db/repositories/`:

- `SignalRepository`
- `OrderRepository`
- `PositionRepository`
- `TradeRepository`
- `EventRepository`
- `ExecutionRepository`
- `BasketRepository`
- `InstrumentRepository` (plus catalog helpers used by resolution)
- `AllocationRepository`

## In-memory vs durable

| Concern | Where |
|---------|-------|
| Active OMS order map / submit dedup | `OMSService` in-memory |
| RMS runtime context (hydrated at startup) | `OrderManager` / `RMSContext` in-memory; seeded from DB; symbol limits reload via `reload_rms_limits()` after config API writes |
| Basket CRITICAL set / live basket objects | `BasketCoordinator` in-memory + `baskets` table |
| Live marks / PnL subscriptions | `LivePnlService` in-memory; writes `positions.live_pnl` |
| Durable ledger | Postgres tables above |
| Webhook raw captures | Files under `backend/data/tradingview_webhooks/` |

`GET /api/v1/orders` reads the **in-memory** OMS map, not `OrderRepository`.

## Redis

- **Main trading package `backend/app/`:** no `redis` imports (verified by search).
- **`demo_streaming/`:** Redis Streams for SSE (`demo_stream_name`, default `positions:stream`). Postgres is polled by `PositionBridge`; Redis fans out to `/demo/stream`.
