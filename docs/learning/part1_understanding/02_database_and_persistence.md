# Chapter 2 — Database & Persistence

## Current Implementation

### Responsibility

PostgreSQL is the system's source of truth. Everything else — in-memory caches,
the SSE stream, frontend stores — is a derived view that must be reconstructable
from the database after a restart. Where that property has been violated, it has
produced bugs; see Chapter 17 and the manual kill-switch cache case study in
[Chapter 16](../part3_engineering_history/16_reliability_fixes.md).

### Schema authority

Alembic, exclusively. 47 migrations under `backend/alembic/versions/` as of
2026-09-21. `Base.metadata.create_all()` is forbidden in application code
(`AGENTS.md` §8).

### Connection pooling

`app/db/session.py` builds two different engines depending on context:

| Context | Pool | Why |
|---|---|---|
| Runtime | `pool_size=20`, `max_overflow=30`, `pool_timeout=30`, `pool_recycle=1800`, `pool_pre_ping=True` | Throughput under fill bursts |
| Test | `NullPool` | Correctness across event loops |

The test/runtime split is not cosmetic. It is discussed under Engineering
History below, because the reason it exists is a real failure mode, and because
a later attempt to apply `NullPool` more widely made things worse.

### Table ownership

Full matrix in [Appendix D](../appendices/D_database_ownership_map.md) and in
[`docs/engineering/03-DATA-OWNERSHIP.md`](../../engineering/03-DATA-OWNERSHIP.md).
The headline rule is that each table has exactly one writing component:

| Table | Sole writer |
|---|---|
| `signal_jobs` | Webhook ingest (insert), `ExecutionWorkerPool` (claim/status) |
| `execution_claims` | `ExecutionClaimRepository` |
| `orders`, `positions` | Engine path via `OrderManager` / `OrderRepository` |
| `manual_orders`, `manual_positions` | `ManualTradingService` / `ManualExecutionListener` |
| `audit_events` | `app/audit/recorder.py` only |
| `broker_positions` | `PositionReconciler` from IBKR snapshots |

### Append-only audit

`audit_events` is enforced append-only by a database trigger, not merely by
convention. The guard function `audit_events_guard()` raises on `DELETE` and on
`UPDATE` of a finalized row:

```
ERROR:  audit_events is append-only: DELETE is not permitted
ERROR:  audit_events row <uuid> is finalized and immutable
```

**Confidence: Confirmed** — both errors reproduced directly against the dev
database on 2026-09-21 inside a rolled-back transaction.

Finalization is a one-way transition: a `PENDING` row may be updated exactly
once to a terminal result, and only its outcome columns. This is what makes the
intent-first recorder in [Chapter 12](../part2_safety_operations/12_operator_audit.md)
trustworthy.

### Failure behavior

- Audit writes that are *required* fail the operation closed with HTTP 503
  rather than proceeding unaudited.
- Row-level locking (`SELECT ... FOR UPDATE`) is mandatory for position
  mutations.
- No broker network calls may be held inside an open database transaction
  (`AGENTS.md` §8). This rule exists because the IBKR round-trip can take
  seconds, and holding a pooled connection that long starves the pool — the
  mechanism behind finding C10 in the 2026-09-03 concurrency review.

### Current limitations

- Concurrent pytest runs against `ibkr_trading_test` can deadlock on
  `TRUNCATE ... CASCADE` (Risk 1 in
  [`18-KNOWN-RISKS.md`](../../engineering/18-KNOWN-RISKS.md)).
- The pool is shared by HTTP handlers, workers and callback persistence. Under a
  fill burst this is a contention point (finding C10, still listed as a
  limitation in [Chapter 21](../part4_ownership/21_current_known_limitations.md)).

---

## Engineering History

### Chronology

| Date | Event | Source |
|---|---|---|
| 2026-08-17 | Alembic introduced | `86485a6` |
| 2026-08-18 | Postgres configuration and initial migrations | `c186d07` |
| 2026-08-20 | Runtime pool configured; `NullPool` adopted for tests | `4f945ca` |
| 2026-09-18 | `audit_events` + `auth_sessions` added, append-only | migration `a7u8d9i0t1r2` |
| 2026-09-21 | Migration branch drift found and resolved on the dev database | this session |

---

# Case Study: Alembic branch drift left the dev database unable to upgrade

**Date:** 2026-09-21
**Feature:** Database / migrations
**Type:** Operational Fix
**Severity:** P2
**Status:** Fixed
**Related Components:** `backend/alembic/versions/`, `alembic_version` table

---

## 1. What Was Happening?

The local development database could not be upgraded to head. Work that
depended on the operator audit trail could not run against it, because the
`audit_events` and `auth_sessions` tables did not exist.

## 2. Expected Behavior

`alembic upgrade head` brings a development database to the current schema.

## 3. Initial Understanding

The initial assumption was that the dev database was simply *behind* — that
several migrations had not been applied and running the upgrade would apply
them. That turned out to be wrong in an important way: the schema changes were
already physically present. Only the version bookkeeping was wrong.

## 4. Symptoms / Evidence

- `SELECT * FROM alembic_version` returned a single row, `c9d0e1f2a3b4`.
- `alembic heads` reported a single head, `a7u8d9i0t1r2`.
- `\dt` showed no `audit_events` and no `auth_sessions` table.
- `alembic history` showed the head as a **mergepoint**:
  `44d33de233a2, c9d0e1f2a3b4 -> a7u8d9i0t1r2 (head) (mergepoint)`.

## 5. Investigation

### Step 1
Listed tables. `audit_events` and `auth_sessions` were absent, confirming the
database was not at head.

### Step 2
Read `alembic history`. The head is a merge of two branches. A mergepoint can
only be applied when *both* parent revisions are stamped. Only one was.

### Step 3
Ruled out "migrations were never applied". Checked columns that belong to the
*unstamped* branch (`44d33de233a2` and its ancestors):

- `manual_positions.status` was `character varying(32)` — the widening from
  `44d33de233a2`
- `kill_switch_operations.scope` existed — from `k1l2s3w4i5t6`
- `accounts.cancel_exposure` existed — from the other branch

Both branches were physically applied. Only the bookkeeping for one was missing.

### Step 4
Root cause identified: `alembic_version` recorded one of the two parents, so
Alembic refused to apply the mergepoint.

## 6. Root Cause

**Confirmed root cause:** the `alembic_version` table recorded only
`c9d0e1f2a3b4`, while the schema contained the effects of both that branch and
the `44d33de233a2` branch. The mergepoint `a7u8d9i0t1r2` requires both parents
stamped.

**Contributing factor:** how the drift arose is **Unknown**. The most likely
explanation is a manual merge or a restored dump, but nothing in the repository
establishes it. Do not record a cause here that has not been verified.

## 7. Code-Level Location

**File:** `backend/alembic/versions/a7u8d9i0t1r2_audit_trail_and_auth_sessions.py`
**Table:** `alembic_version`

**Call path:**

```text
alembic upgrade head
  → resolve revision graph
    → mergepoint a7u8d9i0t1r2 requires {44d33de233a2, c9d0e1f2a3b4}
      → only c9d0e1f2a3b4 stamped
        → upgrade cannot proceed
```

## 8. The Fix

```
alembic stamp 44d33de233a2     # record the branch already physically applied
alembic upgrade head           # apply the mergepoint
```

No migration file was edited and no schema was recreated.

## 9. Verification

`alembic current` reported both parents, then the upgrade ran and created
`audit_events` and `auth_sessions`. Confirmed present via `\dt`.

## 10. Lessons Learned

- A single-head `alembic heads` does **not** mean the graph is linear. A
  mergepoint hides two parents.
- When a database looks "behind", check whether the *columns* are present before
  assuming the migrations are. Stamping and schema can disagree, and the
  remedy is completely different in each case.
- Stamping is safe only after verifying the schema really does contain the
  branch being stamped. Doing it blind would have skipped real migrations.

## 11. Open Questions

- How the drift was introduced is **Unknown**.
- Whether other environments carry the same drift is **not established** — only
  the local development database was inspected.

---

# Case Study: Test engine pooling across event loops

**Date:** 2026-08-20 (original), re-examined 2026-09-21
**Feature:** Test infrastructure / persistence
**Type:** Reliability Fix
**Severity:** P2
**Status:** Fixed (original); a later attempt to widen the same fix was reverted
**Related Components:** `backend/app/db/session.py`, `backend/tests/conftest.py`

---

## 1. What Was Happening?

**Reported:** commit `4f945ca` (2026-08-20) is titled
"fix(db): configure connection pool for runtime app and NullPool for testing",
establishing that pooled connections were a problem under test. The precise
original symptom is **not established by available material** beyond the commit
message.

## 2. Expected Behavior

Test runs should be independent of each other regardless of order.

## 3. Initial Understanding

Not recorded for the 2026-08-20 change.

## 4. Symptoms / Evidence

For the 2026-09-21 re-examination, the evidence was direct:

```
asyncpg.exceptions._base.InterfaceError:
  cannot perform operation: another operation is in progress
```

and, from a different test:

```
RuntimeError: Task <...BlockingPortal._call_func...> got Future
  <Future pending> attached to a different loop
```

## 5. Investigation

Covered in full in the test-pollution case study in
[Chapter 16](../part3_engineering_history/16_reliability_fixes.md), because the
root cause turned out not to be pooling at all.

## 6. Root Cause

**Confirmed:** `NullPool` under test is correct and remains in place for the
runtime/test split in `app/db/session.py`.

**Confirmed, and important:** applying `NullPool` to the shared test
`session_factory` fixture in `conftest.py` in 2026-09-21 fixed 10 failures and
introduced 8 different ones in tests that depend on pooling behaviour. It was
reverted. The real cause of those 10 failures was a leaked FastAPI dependency
override, not pooling.

## 10. Lessons Learned

- `NullPool` is the right default for a *test engine*, and the wrong instrument
  for fixing cross-test contamination generally.
- A fix that resolves the failures you were looking at while breaking tests you
  were not looking at is not a fix. Always re-run the whole suite, not the
  subset you were debugging.

## 11. Open Questions

- The exact 2026-08-20 symptom is **Unknown**.
- Why eight specific tests depend on pooled connection reuse is **not
  established**; they were observed to fail under `NullPool` and the change was
  reverted rather than investigated further.
