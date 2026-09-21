# Chapter 4 — Worker Execution

## Current Implementation

### Responsibility

Claim queued signal jobs exactly once, hold a lease while executing, and record
a truthful terminal status — including when the worker itself lost the right to
continue.

### Major components

| Component | File | Role |
|---|---|---|
| `ExecutionWorkerPool` | `backend/app/services/worker_pool.py` | Claim loop, leases, heartbeats |
| `SignalJobRepository` | `backend/app/db/repositories/signal_repository.py` | `claim_next_jobs`, `heartbeat_lease`, fenced status writes |
| `ExecutionClaimRepository` | `backend/app/db/repositories/execution_claim_repository.py` | Durable dedup barrier |

### Claiming

`claim_next_jobs` selects with `FOR UPDATE SKIP LOCKED` and performs the
`UPDATE ... SET status='CLAIMED'` **in the same transaction**. It is genuinely
atomic, not a read-then-write. The 2026-09-03 concurrency review verified this
explicitly and listed it under "what is already correct".

### Leases and fencing

A claimed job carries a lease. `_lease_heartbeat` renews it periodically; if
renewal fails, `lease_lost` is set and the executing coroutine declines to
continue.

Terminal status writes are **fenced on `worker_id`**. The review traced the
takeover scenario end to end and confirmed the fence holds: if a second worker
reclaims the job, the first worker's fenced write matches zero rows, sets
`lease_lost`, and returns *before touching the broker*.

### The durable dedup barrier

Engine orders must commit an `execution_claims` row **before** submitting to
IBKR (ADR 3). In-process sets are lost on crash; the database row is not. On the
webhook path this is the mechanism that actually prevents double submission.

### State transitions

```text
PENDING → CLAIMED → PROCESSING → PROCESSED
                              ↘ REJECTED
                              ↘ RECOVERY_REQUIRED   (exception, no orders emitted)
                              ↘ QUARANTINED         (exception, orders already emitted)
```

The distinction between the last two is deliberate and is covered by
`backend/tests/test_worker_recovery_classification.py`: a job that died *after*
emitting orders cannot be safely retried, so it is quarantined rather than
recovered.

### Failure behavior

Fails closed. A worker that cannot prove it still owns the job stops before
placing orders.

### Current limitations

- **Finding C4 (P1, "likely", 2026-09-03):** the OPEN-before-CLOSE
  serialisation in `claim_next_jobs` uses a `NOT EXISTS` sibling test that
  cannot see another worker's uncommitted `status='CLAIMED'`. Two workers
  claiming concurrently can therefore run the CLOSE first, which rejects with
  `NO_OPEN_POSITION`, leaving the position open with its CLOSE consumed.
  Whether this has been fixed since is **not established** — see below.
- **Finding C10 (P2):** callback persistence schedules unbounded coroutines,
  each taking a pooled connection. Under a fill burst, heartbeats can time out
  on connection checkout while the worker is alive.

---

## Engineering History

### Chronology

| Date | Change | Commit / source |
|---|---|---|
| 2026-08-21 | Durable queuing with async worker pool | `4c47e37` |
| 2026-09-02 | Non-transmitting concurrency and order-transmission boundary tests | `4f6f1d3` |
| 2026-09-03 | Concurrency review: findings C1–C15 | `448a9e2` |
| 2026-09-03 | MFT concurrency recovery tests | `test_mft_concurrency_recovery.py` |

---

# Case Study: Heartbeat exception left a worker running on a lost lease

**Date:** Reported 2026-09-03 (finding C11); fix confirmed present 2026-09-21
**Feature:** Worker execution
**Type:** Reliability Fix
**Severity:** P2
**Status:** Fixed
**Related Components:** `ExecutionWorkerPool._lease_heartbeat`, `signal_jobs`

---

## 1. What Was Happening?

**Reported** by `docs/review/BUGS-concurrency.md` (2026-09-03), finding C11:

> Heartbeat raises (pool timeout, transient DB error) rather than returning
> `False`. The `except Exception` arm logs and loops; it never sets
> `lease_lost`, unlike the `not renewed` arm.

Observable symptom, as reported: a worker keeps placing orders on a lease the
reclaimer has already taken, and the job lands in `RECOVERY_REQUIRED` with live
orders outstanding.

Confidence as recorded by the review: **certain**.

## 2. Expected Behavior

Any failure to *prove* the lease is still held — whether the renewal returned
false or the renewal itself threw — must set `lease_lost` and stop the worker
before it places further orders.

## 3. Initial Understanding

The review's framing is the initial understanding, and it was correct: the code
distinguished "renewal said no" from "renewal blew up", and only treated the
first as lease loss. There is no record of a competing theory.

## 4. Symptoms / Evidence

The finding is a code-reading result, not an incident report. No production
occurrence is recorded in available material. This matters: the severity is
based on reachability, not on observed loss.

## 5. Investigation

### Step 1
Review traced the two exit arms of the heartbeat loop.

### Step 2
Found the `not renewed` arm sets `lease_lost` and breaks; the `except Exception`
arm logged and continued looping.

### Step 3
Identified the reachable trigger: `pool_timeout=30` on a pool shared with
callback persistence (finding C10) makes a heartbeat exception realistic under
load, not hypothetical.

### Step 4
Concluded the two failure modes needed identical handling.

## 6. Root Cause

**Confirmed root cause:** asymmetric handling of two equivalent failures. A
heartbeat that throws proves nothing about lease ownership, but was treated as
"keep going".

**Contributing factor:** shared connection pool contention (C10) makes the
exception path reachable.

## 7. Code-Level Location

**File:** `backend/app/services/worker_pool.py`
**Class:** `ExecutionWorkerPool`
**Function:** `_lease_heartbeat()`

**Call path:**

```text
ExecutionWorkerPool._run_worker()
  → _lease_heartbeat()
    → SignalJobRepository.heartbeat_lease()
      → raises (pool timeout / transient DB error)
        → except Exception: logged, loop continued   [defect]
```

## 8. The Fix

The exception arm now sets `lease_lost` and breaks, matching the `not renewed`
arm:

```python
except Exception:  # noqa: BLE001
    logger.warning(
        "Worker %s failed to renew heartbeat for job %s; treating as lease_lost",
        worker_id, job_id,
    )
    lease_lost.set()
    break
```

## 9. Verification

**Confirmed 2026-09-21** by reading `worker_pool.py` at the current commit. The
fix is present. The commit that introduced it is **not established** — it was
verified by current state, not traced through history.

## 10. Lessons Learned

- When a check has two failure modes — "answered no" and "could not answer" —
  they almost always need the same handling. Treating "could not answer" as
  success is a recurring shape of bug, and it appears again in the notification
  and reconciliation chapters.
- Fail-closed means fail-closed on the error path too, which is the path least
  likely to be exercised in testing.

## 11. Open Questions

- Which commit applied the fix is **Unknown**.
- Finding C4 (OPEN/CLOSE claim ordering) has **not** been re-verified. Its
  status is **Unknown** and it is carried forward into
  [Chapter 21](../part4_ownership/21_current_known_limitations.md).
