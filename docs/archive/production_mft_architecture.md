# Target Production MFT Signal Ingestion & Execution Architecture

This document defines the production-grade MFT execution architecture capable of absorbing 300+ TradingView signals arriving in a burst without blocking HTTP webhooks, losing state, or altering trading decisions.

---

## 1. High-Level Target Architecture Flow Diagram

```
                              TradingView Alert Burst (300+ Signals)
                                                │
                                                ▼
                             ┌─────────────────────────────────────┐
                             │    Fast Webhook Ingress (FastAPI)   │
                             │  - Parse JSON                       │
                             │  - Generate Request & Idempotency ID│
                             │  - Non-blocking disk log            │
                             │  - Persist to Signal Inbox (Postgres)│
                             └──────────────────┬──────────────────┘
                                                │
                                       Immediate HTTP 202 ACK
                                    {"status": "accepted", ...}
                                                │
                                                ▼
                             ┌─────────────────────────────────────┐
                             │       Durable PostgreSQL Inbox      │
                             │        (State Machine Queue)        │
                             └──────────────────┬──────────────────┘
                                                │
                                                │ FOR UPDATE SKIP LOCKED
                                                ▼
                             ┌─────────────────────────────────────┐
                             │       Execution Worker Pool         │
                             │    (Configurable Concurrency N)     │
                             └──────────────────┬──────────────────┘
                                                │
                 ┌──────────────────────────────┴──────────────────────────────┐
                 │ Partitioned Execution per (account_id, strategy_id)        │
                 ▼                                                             ▼
┌──────────────────────────────────┐                         ┌──────────────────────────────────┐
│  Worker Thread / Async Task 1    │                         │  Worker Thread / Async Task N    │
│  - Load Strategy Handler         │                         │  - Load Strategy Handler         │
│  - Account Fan-out (Parallel)    │                         │  - Account Fan-out (Parallel)    │
│  - RMS Checks (Existing Logic)   │                         │  - RMS Checks (Existing Logic)   │
│  - Isolated Basket Context       │                         │  - Isolated Basket Context       │
└────────────────┬─────────────────┘                         └────────────────┬─────────────────┘
                 │                                                            │
                 └──────────────────────────────┬─────────────────────────────┘
                                                │
                                                ▼
                             ┌─────────────────────────────────────┐
                             │   Centralized IBKR Rate Scheduler   │
                             │  - Token Bucket Order Pacing        │
                             │  - Bounded Broker Concurrency Gate  │
                             │  - Priority Queueing                │
                             └──────────────────┬──────────────────┘
                                                │
                                                ▼
                             ┌─────────────────────────────────────┐
                             │      IBKR TWS / Gateway Socket      │
                             └──────────────────┬──────────────────┘
                                                │
                                                ▼
                             ┌─────────────────────────────────────┐
                             │ Persistent Execution Reconciliation │
                             │  - Post-restart Recovery Manager    │
                             │  - Fill / Callback Correlation      │
                             │  - Live SSE PnL Dashboard Streaming │
                             └─────────────────────────────────────┘
```

---

## 2. Ingestion Boundary vs. Execution Engine Decoupling

The primary architectural shift is the complete decoupling of **Signal Ingestion** from **Signal Execution**:

| Aspect | Ingestion Boundary (`POST /api/webhooks/tradingview`) | Execution Engine (`ExecutionWorkerPool`) |
|---|---|---|
| **Latency Goal** | < 15ms (p99) | Depends on broker fill (100ms - 90s) |
| **Blocking Dependencies** | PostgreSQL atomic INSERT only | Strategy sizing, RMS, TWS submission, fill wait, retries, compensation |
| **Failure Handling** | Returns HTTP 400 (malformed) or 500 (DB down) | Retries, square-off, compensation, state machine transitions |
| **HTTP Response** | Returns HTTP 202 `{"status": "accepted", "job_id": "...", "signal_id": "..."}` | Pushes live SSE updates to dashboard UI |

---

## 3. Signal Inbox Job State Machine

Each signal ingestion creates a durable job entry in the `signal_jobs` / `signals` table with the following state machine transitions:

```
[ RECEIVED ]
     │
     ▼
[ QUEUED ] ──(Worker Claims via FOR UPDATE SKIP LOCKED)──► [ CLAIMED ]
                                                               │
                                                               ▼
                                                        [ PROCESSING ]
                                                               │
                       ┌───────────────────────────────────────┼───────────────────────────────────────┐
                       │                                       │                                       │
                       ▼                                       ▼                                       ▼
                 [ COMPLETED ]                           [ REJECTED ]                            [ FAILED ]
           (Filled / Compensated)                     (RMS / Validation)                     (Max Retries Exceeded)
                                                                                                       │
                                                                                                       ▼
                                                                                             [ RECOVERY_REQUIRED ]
                                                                                                       │
                                                                                                       ▼
                                                                                                 [ DEAD_LETTER ]
```

### State Definitions:

- **`RECEIVED`**: Ingested by webhook, initial validation passed.
- **`QUEUED`**: Written to durable inbox, ready for worker processing.
- **`CLAIMED`**: Claimed by a worker process using a row lock lease (`worker_id`, `lease_expires_at`).
- **`PROCESSING`**: RMS passed, orders active in OMS / IBKR.
- **`COMPLETED`**: Execution lifecycle finished (filled, closed, or compensated).
- **`REJECTED`**: Rejected by RMS check or invalid account routing (terminal state).
- **`FAILED`**: Exception during execution or connection drop; eligible for retry/recovery.
- **`RECOVERY_REQUIRED`**: Unclear broker state after crash; requires recovery reconciliation before re-submitting.
- **`DEAD_LETTER`**: Exceeded max recovery attempts; flagged for manual operator inspection.

---

## 4. Key Target Components & Responsibilities

### Component 1: Ingestion API Endpoint
- Fast JSON parsing and schema validation.
- Calculation of deterministic idempotency key (`sha256(strategy_id + signal_id + payload_body)`).
- Atomic insert into `signals` and `signal_jobs` tables using `ON CONFLICT DO NOTHING`.
- Non-blocking asynchronous file capture off the event loop.

### Component 2: Execution Worker Pool (`WorkerPool`)
- Manages `N` concurrent asyncio worker tasks.
- Polls PostgreSQL using `SELECT ... FOR UPDATE SKIP LOCKED` to safely claim queued jobs across multiple workers without lock contention.
- Periodically renews worker leases (`lease_expires_at`).
- Detects stale/expired worker leases and safely re-queues abandoned jobs.

### Component 3: Execution Context & Basket Isolation
- Replaces shared `BasketCoordinator._active_basket` with thread-safe/task-isolated `ExecutionContext` objects.
- Each basket execution maintains its own isolated order state dictionary and fill listeners.

### Component 4: Non-Blocking Contract Details Resolver
- Replaces blocking `threading.Event().wait()` in CFD discovery with `asyncio.to_thread` or an `asyncio.Event` completion mechanism to ensure the FastAPI event loop is never frozen.

### Component 5: Centralized IBKR Execution Scheduler
- Token bucket / leaky bucket request rate limiter protecting the TWS socket.
- Enforces max order placement rate (e.g., max 40 orders/sec to TWS) and contract query rate.
- Priority queueing: Cancellations & Compensations > OPEN orders > Contract details queries.

### Component 6: Process Recovery Manager
- Runs on backend process startup before processing new queued signals.
- Inspects any jobs in `CLAIMED`, `PROCESSING`, or `RECOVERY_REQUIRED` states.
- Queries TWS order snapshot (`reqAllOpenOrders` / `reqExecutions`) to reconcile in-flight broker orders before making any new submission decisions.

---

## 5. Preservation of Trading Decisions

All business calculations—including `ModelBlueSizer`, `RMSEngine` checks 2/3/4/7/8, basket leg construction, partial-fill logic, auto-square-off retry windows, and compensation square-off logic—remain 100% untouched.

---
*Target Architecture Specification.*
