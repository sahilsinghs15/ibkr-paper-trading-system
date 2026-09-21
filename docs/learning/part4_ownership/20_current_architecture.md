# Chapter 20 — Current Architecture

State as of commit `64f3009`, 2026-09-21.

## Processes

```text
TradingView
    │ webhook
    ▼
┌──────────────────────┐
│ webhook-ingest :8000 │  Postgres only. Never touches the broker.
└──────────┬───────────┘
           │ INSERT signal_jobs
           ▼
     ┌───────────┐
     │ PostgreSQL│◄──────────────┐
     └─────┬─────┘               │
           │ claim               │
           ▼                     │
┌──────────────────────┐         │
│ trading-backend :8001│         │
│  ExecutionWorkerPool │         │
│  OrderManager        │         │
│  RMSEngine           │         │
│  BasketCoordinator   │         │
│  OMSService          │         │
│  KillSwitchService   │         │
│  ManualTradingService│         │
│  LivePnlService      │         │
│  PositionReconciler  │         │
│  ─────────────────── │         │
│  TWSClient (1 socket)│──────► IB Gateway :4002
│  + reader thread     │         │
└──────────┬───────────┘         │
           │                     │
┌──────────▼───────────┐         │
│ demo-streaming :8010 │─────────┘
│  SSE + snapshots     │
└──────────┬───────────┘
           │ SSE / REST
           ▼
      React frontend

┌──────────────────────┐
│ watchdog             │  health, recovery, safety gate
└──────────────────────┘
```

## Request path — engine order

```text
POST /webhook (:8000)
  → INSERT signal_jobs (PENDING)                     [durability boundary]
  → ExecutionWorkerPool.claim_next_jobs()            FOR UPDATE SKIP LOCKED
    → OrderManager.process_signal_execution()
      → SessionClock.projected_in_red_zone()         gate BEFORE fan-out
      → account fan-out (per enabled account)
        → RMSEngine.evaluate()                       checks 1,2,3,4,7,8,101
          → execution_claims INSERT + COMMIT          [dedup barrier]
            → BasketCoordinator.execute()
              → OMSService.submit_one_leg()
                → GatewayRateLimiter.acquire()
                  → TWSClient.placeOrder()
```

Callbacks return on the reader thread:

```text
TWSClientThread
  → orderStatus / execDetails / commissionReport
    → IBKRExecutionAdapter
      → OrderRepository / ExecutionRepository
      → PositionRepository  (engine)  |  ManualExecutionListener (manual)
        → LivePnlService marks
          → demo_streaming publisher → SSE → frontend
```

## Request path — manual order

```text
POST /manual/orders
  → ManualTradingService.submit_order()
    → RMS / margin checks
      → INSERT manual_orders (PENDING_SUBMIT) + COMMIT   [idempotency barrier]
        → unique violation? → rollback, re-read, compare params
                              → same → idempotent replay (no broker order)
                              → diff → HTTP 409
        → TWSClient.placeOrder()
```

## Sole owners

| Concern | Owner |
|---|---|
| Engine orders | `OrderManager` → `BasketCoordinator` → `OMSService` |
| Manual orders | `ManualTradingService` |
| Manual fills & positions | `ManualExecutionListener` |
| Engine positions | `OrderManager._update_runtime_state` |
| Broker interface | `TWSClient` |
| Outbound pacing | `GatewayRateLimiter` |
| Marks & unrealized PnL | `LivePnlService` |
| Reconciliation | `PositionReconciler` |
| Emergency flattening | `KillSwitchService` |
| Operator audit | `app/audit/recorder.py` |

## Authoritative vs derived

**Authoritative (database):** `signal_jobs`, `execution_claims`, `orders`,
`manual_orders`, `positions`, `manual_positions`, `kill_switch_operations`,
`accounts.trading_paused`, `manual_halt_state`, `audit_events`.

**External reality:** IBKR `position()` → `broker_positions`; `execDetails` →
`executions`; `tickPrice` stream.

**Derived — never authoritative:** frontend stores, SSE caches,
`positions.live_pnl`, `manual_positions.live_pnl`,
`_KILL_SWITCH_ACTIVE_ACCOUNTS`, `_MANUAL_KILL_SWITCH_ACTIVE_ACCOUNTS`,
notification flapping windows.

Every item in the third list must be reconstructable from the first two. Where
that failed, it produced a defect — see Chapter 10.
