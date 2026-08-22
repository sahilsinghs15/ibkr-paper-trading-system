# Backend concurrency — jobs, workers, claims, recovery

**Verified from:** `backend/app/services/worker_pool.py`, `backend/app/services/recovery.py`, `backend/app/services/order_manager.py`, `backend/app/db/models/signal.py`, `backend/app/db/models/execution_claim.py`, `backend/app/db/repositories/signal_repository.py`, `backend/app/db/repositories/execution_claim_repository.py`, `backend/app/core/identifiers.py`, `backend/app/main.py`.

Use this when debugging duplicate execution, stuck jobs, lease loss, or crash recovery. For the full pipeline see [`backend-execution.md`](backend-execution.md).

## Architecture overview

```
POST /api/webhooks/tradingview  →  signal_jobs (QUEUED)
                                        ↓
ExecutionWorkerPool (10 workers)  →  lease + domain lock + heartbeat
                                        ↓
OrderManager.process_signal_execution  →  RMS → resolve → claim → basket → IBKR
                                        ↓
execution_claims (CLAIMED → EXECUTED)
```

HTTP **202 `accepted`** means the job was enqueued — **not** that it filled. Check `signal_jobs.status` for execution outcome.

## Signal jobs (`signal_jobs`)

**Model:** `SignalJobModel` in `db/models/signal.py` (import from that module; not exported in `db/models/__init__.py`).

**Repo:** `SignalJobRepository` in `db/repositories/signal_repository.py`.

### Idempotency key

`compute_idempotency_key(payload)` in `worker_pool.py`:

1. `strategy_id` = `normalize_strategy_id(payload.strategy | payload.strategy_id)` — lowercase
2. `trade_id` = `normalize_trade_id(payload.trade_id | payload.signal_id)` — case preserved
3. `action` = uppercase strip of `payload.action`
4. `signal_id` = `trade_id`; for CLOSE append `:CLOSE` if missing
5. `idempotency_key` = `sha256(f"{strategy_id}:{signal_id}:{action}")`

Duplicate webhooks with the same key return the existing job (no second row). Changing normalization requires a data backfill (migration `a4c7e2f10938`).

### Job status machine

```
RECEIVED / QUEUED
  → CLAIMED (lease acquired)
  → PROCESSING (execution started)
  → COMPLETED | REJECTED | FAILED
  ↘ RECOVERY_REQUIRED (lease died mid-exec, or crash with orders emitted)
  ↘ DEAD_LETTER (max attempts exceeded)
```

| Status | Meaning |
|--------|---------|
| `QUEUED` | Ready for a worker to claim |
| `CLAIMED` | Worker holds lease, not yet marked PROCESSING |
| `PROCESSING` | Worker actively executing |
| `COMPLETED` | Pipeline finished successfully |
| `REJECTED` | Parse failure or RMS/OMS policy rejection |
| `FAILED` | Execution incomplete or unhandled exception |
| `RECOVERY_REQUIRED` | Quarantined — orders may exist; needs reconciliation |
| `DEAD_LETTER` | Exceeded `max_attempts` (default 3) |

**Invariant:** `ACTIVE_LEASE_STATUSES = (CLAIMED, PROCESSING)` must be used consistently in claim, heartbeat, reclaim, and fenced status writes. Omitting `PROCESSING` from any predicate silently breaks lease maintenance.

### Claiming (`claim_next_jobs`)

- Uses `FOR UPDATE SKIP LOCKED`, oldest `received_at` first
- Claimable: `QUEUED` / `RECEIVED`, or expired `CLAIMED` / `PROCESSING` leases
- **Same `trade_id` siblings blocked** while another job holds an active lease — OPEN runs before CLOSE
- Sets `worker_id`, `claimed_at`, `lease_expires_at`, increments `attempt_count`

### Lease heartbeat

- Background task renews lease every `max(2.0, lease_duration / 3)` (default ~10s)
- If renewal fails → `lease_lost` event; worker must **not** write terminal status
- Fenced writes: `update_status(..., fence=True, worker_id=...)` — returns 0 rows if lease lost

### Reclaimer loop (every 15s)

| Expired status | Action |
|----------------|--------|
| `CLAIMED` | Requeue → `QUEUED` |
| `PROCESSING` | Quarantine → `RECOVERY_REQUIRED` (do **not** blind-requeue mid-exec) |
| Max attempts | → `DEAD_LETTER` |

Also runs `ExecutionClaimRepository.reconcile_stale_claims(stale_after_sec=300)`.

## Worker pool (`ExecutionWorkerPool`)

**File:** `services/worker_pool.py`. Started in `main.py` with `worker_count=10`.

| Parameter | Default |
|-----------|---------|
| `lease_duration_sec` | 30 |
| `reclaim_interval_sec` | 15 |
| `claim_stale_after_sec` | 300 |

### Domain lock

Per `(account_scope || "default", strategy_id)` — serializes jobs for the same strategy partition so RMS/exposure state does not interleave.

### Exposure lock (separate)

Per `(account_id, symbol)` in `OrderManager._exposure_guard` — serializes money-per-stock read-modify-write across strategies on the same symbol. **Domain lock does not replace this.**

### Worker flow

1. `claim_next_jobs(limit=1)`
2. Acquire domain lock
3. Re-check lease (may have been reclaimed while waiting on lock)
4. Start heartbeat task
5. `_write_status(PROCESSING)` with fence
6. `order_manager.process_signal_execution(domain_signal)`
7. Write terminal status (COMPLETED / REJECTED / FAILED) only if lease still held
8. Stop heartbeat, clear log context

## Execution claims (`execution_claims`)

**Model:** `ExecutionClaimModel` in `db/models/execution_claim.py`.

**Repo:** `ExecutionClaimRepository` in `db/repositories/execution_claim_repository.py`.

Durable dedupe barrier across processes and crashes. `RMSContext.processed_signals` is in-memory only and written **after** success — it cannot stop a replay that crashed mid-execution.

### Dedupe key

`execution_dedupe_key(intent)` = `{account_id or '-'}:{strategy_id}:{signal_id}`

CLOSE intents use `signal_id = trade_id:CLOSE`.

### States

| State | Meaning |
|-------|---------|
| `CLAIMED` | Right to execute acquired; work in progress |
| `EXECUTED` | Permanent barrier — never re-execute |
| `ABANDONED` | Released for retry (only when zero orders emitted) |

### Acquire timing

Claim is acquired **after** RMS PASS + CRITICAL gate + instrument resolve, **before** basket/`placeOrder`, in its **own** committed transaction (`OrderManager._acquire_execution_claim`).

Seal (`mark_executed`) on settled OPEN/CLOSED basket. Release only when `count_orders_emitted == 0`.

### Exceptions

| Exception | Meaning | Agent action |
|-----------|---------|--------------|
| `DuplicateExecutionError` | Already EXECUTED | Never retry |
| `ExecutionInFlightError` | Another worker holds CLAIMED (< stale threshold) | Wait or investigate |
| `ClaimNeedsReconciliationError` | Stale CLAIMED, broker state unknown | Reconcile before retry |

### Reconciliation (`reconcile_stale_claims`)

For CLAIMED rows older than cutoff:

- Orders emitted → seal `EXECUTED` (work was done)
- No orders → release `ABANDONED` (safe to retry)

Runs on startup (`stale_after_sec=0`) and periodically from worker reclaimer (300s).

## Recovery (`RecoveryManager`)

**File:** `services/recovery.py`. Runs in `main.py` **before** worker pool starts.

### Scans

- Jobs in `CLAIMED` / `PROCESSING` / `RECOVERY_REQUIRED`
- Baskets in `EXECUTING` / `UNWINDING`

### Per-job decision

1. Reconcile all CLAIMED execution claims (`stale_after_sec=0`)
2. If `count_orders_emitted(strategy_id, signal_id) > 0` → stay/set `RECOVERY_REQUIRED` (never requeue)
3. Else if `attempt_count >= max_attempts` → `DEAD_LETTER`
4. Else → requeue `QUEUED`

Best-effort broker snapshot (`fetch_broker_order_snapshot`) is fire-and-forget — must not gate the decision.

Then re-runs `hydrate_runtime_from_db()` (critical baskets, kill-switch cache, RMS state).

## Separate inbox vs job queue

| Table | Purpose | Statuses |
|-------|---------|----------|
| `signals` (`SignalModel`) | Audit inbox per strategy+signal_id | NEW / PROCESSED / REJECTED |
| `signal_jobs` (`SignalJobModel`) | Durable execution queue | See job status machine above |

Workers drive execution; `SignalRepository` records inbound audit regardless.

## Log greps

| Grep | Stage |
|------|--------|
| `Webhook HTTP 202 accepted` | Job enqueued |
| `Duplicate webhook received for idempotency_key` | Idempotent replay |
| `ExecutionWorkerPool started with` | Pool ready |
| `Worker .* starting execution for job_id` | Worker picked up job |
| `Worker .* LOST its lease` | Lease fencing triggered |
| `Stale lease sweep` | Reclaimer activity |
| `Orphaned claim sweep` | Claim reconciliation |
| `Acquired execution claim` | Pre-broker barrier taken |
| `Sealed stale execution claim` | Reconciliation promoted claim |
| `Released stale execution claim` | Reconciliation released claim |
| `Recovery requeued job_id` | Startup safe retry |
| `Recovery quarantined job_id` | Startup — orders already emitted |
| `Startup recovery scan found` | Recovery scanner summary |

## Do not break (agent invariants)

1. Never re-send an intent that already has order rows in `orders`.
2. Never write terminal job status after `lease_lost`.
3. Never release an execution claim if any order was emitted.
4. Never requeue a job in `PROCESSING` with expired lease — use `RECOVERY_REQUIRED`.
5. Keep `ACTIVE_LEASE_STATUSES` consistent everywhere.
6. Do not construct a second `ExecutionWorkerPool` in a request handler.
7. HTTP 202 `accepted` is not a fill confirmation.
8. Changing `normalize_strategy_id` or idempotency inputs requires migration/backfill.
