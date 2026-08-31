# Production MFT Concurrency Model & State Isolation

This document specifies the exact 5-level concurrency model for handling 300+ burst signals safely without state corruption or race conditions.

---

## 1. Five Levels of Concurrency

```
  Level 1: INGRESS CONCURRENCY (300+ HTTP Requests/sec via Async Ingress)
                             │
                             ▼
  Level 2: WORKER CONCURRENCY (Bounded Worker Pool, e.g., 10-20 Async Workers)
                             │
                             ▼
  Level 3: PARTITIONED DOMAIN SAFETY (Key: account_id + strategy_id)
                             │
                             ▼
  Level 4: ACCOUNT FAN-OUT CONCURRENCY (Parallel execution across accounts)
                             │
                             ▼
  Level 5: CENTRALIZED BROKER SCHEDULER (Bounded IBKR Rate Limiter & Concurrency Gate)
```

---

## 2. Detailed Level Specifications

### Level 1: Ingress Concurrency (Fast Ingestion Boundary)
- **Objective**: Instantly absorb 300+ incoming TradingView webhooks arriving in a burst (< 15ms p99 response).
- **Mechanism**: FastAPI handles inbound HTTP requests asynchronously. The request handler performs lightweight JSON parsing, generates deterministic idempotency keys, persists the signal to PostgreSQL, and returns an HTTP 202 ACK immediately.
- **Backpressure**: Database pool size bounds ingress writing. If DB connection pool is exhausted, PostgreSQL returns standard queueing without dropping requests.

### Level 2: Worker Concurrency (Execution Worker Pool)
- **Objective**: Limit active signal processing tasks to a configurable number `N` (e.g. 10 workers).
- **Mechanism**: Workers fetch jobs using `SELECT ... FOR UPDATE SKIP LOCKED`. Multiple worker tasks poll concurrently without blocking one another or attempting to execute the same signal twice.
- **Configurability**: Configured via `WORKER_POOL_SIZE` setting in `backend/app/core/config.py`.

### Level 3: Domain Safety / Account-Strategy Partitioning
- **Objective**: Prevent race conditions when multiple signals for the *same* `(account_id, strategy_id)` arrive simultaneously.
- **Mechanism**: In-memory async lock pool keyed by `(account_id, strategy_id)`.
  - Signal processing for *different* account-strategy pairs runs completely in parallel.
  - Signals for the *same* account-strategy pair execute sequentially in received order to preserve accurate RMS position counts and exposure tracking.

### Level 4: Account Fan-Out Concurrency
- **Objective**: Accelerate account fan-out when a single strategy signal routes to multiple accounts (e.g., Accounts A, B, C, D).
- **Mechanism**: Replaces the sequential `for ctx in contexts:` loop in `OrderManager._fanout_accounts()` with `asyncio.gather()` / concurrent task execution.
- **Isolation**: A delay or fill timeout on Account A will no longer block execution on Account B or Account C.

### Level 5: Centralized IBKR Scheduler Concurrency
- **Objective**: Prevent broker socket saturation or TWS order rejection rate-limits when processing burst signals.
- **Mechanism**: Bounded concurrency semaphore (e.g., max 10 concurrent active broker calls) combined with a leaky bucket rate limiter (e.g., max 40 orders/sec).

---

## 3. Elimination of Shared Mutable Basket State

### Problem in Baseline
In the legacy implementation, `BasketCoordinator` contained:
```python
class BasketCoordinator:
    def __init__(self, ...):
        self._active_basket: Basket | None = None
```
When two baskets executed concurrently, `self._active_basket` was mutated by whichever basket ran last, corrupting fill tracking and compensation logs.

### Target Refactored Design
Every execution produces an isolated `BasketContext` instance passed explicitly through the execution stack:
```python
@dataclass
class BasketContext:
    basket: Basket
    submitted_orders: list[OMSOrder] = field(default_factory=list)
    order_baskets: dict[str, Basket] = field(default_factory=dict)
```
- No shared `_active_basket` attribute on `BasketCoordinator`.
- `BasketCoordinator` retains only shared thread-safe registries (`_critical` set protected by async locks, `_session_factory`).

---

## 4. Race Condition & Lock Matrix

| Scenario | Risk | Mitigation |
|---|---|---|
| 2 duplicate webhooks hit API simultaneously | Both try to create execution intents | Database unique constraint on `(strategy_id, signal_id, idempotency_key)` guarantees atomic single insert. Second request gets existing job ID. |
| 2 workers try to claim the same queued job | Duplicate order submission | PostgreSQL `SELECT ... FOR UPDATE SKIP LOCKED` guarantees only 1 worker claims a job row. |
| Worker crashes while processing job | Job left in `CLAIMED` state forever | Worker heartbeat updates `lease_expires_at`. Dead worker leases are reclaimed by lease monitor after timeout. |
| 2 signals for same account execute concurrently | Corrupted RMS exposure count | Partitioned lock `(account_id, strategy_id)` serializes same-account processing. |

---
*Concurrency Model Specification.*
