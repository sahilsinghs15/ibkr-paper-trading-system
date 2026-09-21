# Chapter 12 — Operator Audit

## Current Implementation

### Responsibility

Record every state-changing operator action with the identity that performed it,
in a form that cannot later be altered. New state-changing operator endpoints
must record through `app/audit/recorder.py`, never via `event_log`
(`AGENTS.md` §8).

### Components

| Component | File |
|---|---|
| Taxonomy | `backend/app/audit/taxonomy.py` |
| Recorder | `backend/app/audit/recorder.py` |
| Request/actor context | `backend/app/audit/context.py` |
| Sanitisation | `backend/app/audit/sanitize.py` |
| Repository | `backend/app/db/repositories/audit_repository.py` |
| API | `backend/app/api/routes/audit.py` |
| UI | `frontend/src/components/audit/` |

### Categories

Nine, enforced by a database CHECK constraint. Actions are validated in Python
so new operations do not require a migration.

| Category | Actions |
|---|---|
| `AUTHENTICATION` | 4 |
| `AUTHORIZATION` | 1 |
| `ACCOUNT_ADMIN` | 2 |
| `SETTINGS` | 8 |
| `INVENTORY` | 6 |
| `MANUAL_TRADING` | 3 |
| `POSITIONS` | 2 |
| `EMERGENCY` | 7 |
| `SYSTEM_CONTROL` | 4 |

Inventory fix actions record the **specific fix type** —
`INVENTORY_FIX_QUANTITY_MISMATCH`, `INVENTORY_FIX_LEDGER_GHOST`,
`INVENTORY_FIX_BROKER_GHOST`, `INVENTORY_FIX_UNCLASSIFIED` — mapped from
reconcile diff kinds by `INVENTORY_FIX_ACTIONS`. `INVENTORY_FIX_ALIGN` exists
only for legacy import, where the fix type was not recorded.

### Three write modes

The distinction is the core of the design:

| Mode | Semantics | Used for |
|---|---|---|
| `record(session, entry)` | Joins the caller's transaction — audit row and state change commit together or neither does | Single-transaction mutations (settings) |
| `record_committed(entry)` | Own short transaction; best-effort, never raises | Security events, post-rollback rejections |
| `operation(entry)` | **Intent-first**: row committed as `PENDING` *before* the side effect, finalized exactly once afterwards | Broker orders, flattens, service lifecycle |

`operation(..., required=True)` fails closed with HTTP 503
(`AuditUnavailableError`) if the intent cannot be made durable. The operation is
then not executed at all.

If the process dies mid-operation, the committed `PENDING` row remains as
durable evidence that the action was requested — which is the entire point.

### Service control ordering

`app/api/routes/service_control.py` makes the guarantee explicit in a comment:

> ORDERING GUARANTEE: `operation()` commits the PENDING audit row in its own
> transaction and only then yields. `systemctl` is never invoked unless that
> commit succeeded (`required=True` fails closed with HTTP 503).

`SELF_UNIT = "trading-backend.service"`; stopping or restarting it is
`self_terminating`, and `systemctl --no-block` is used so the call returns once
the job is queued. This satisfies the requirement that audit logs be persisted
and committed before the backend shuts itself down.

### Immutability

Enforced by database trigger `audit_events_guard()`. `DELETE` is refused
outright; `UPDATE` of a finalized row is refused. A `PENDING` row may transition
to a terminal result exactly once, and only its outcome columns.

### Search

`GET /audit/events` supports filtering by date range, actor e-mail, actor user
id, role, IP (address or CIDR), category (repeatable), action (repeatable),
result (repeatable), account, session id, device id, target type/id, ref id,
order id, trade id, position id, correlation id, browser, keyword, and
provenance, with sort and pagination. `GET /audit/facets` returns the filter
vocabularies. Both are admin-only and read-only — there are deliberately no
write, modify or delete routes.

### Identity capture

Every row carries `actor_type`, `actor_email`, `actor_role`, `client_ip`,
`session_id`, `auth_method`, `http_method`, `http_path`, and a client label
derived from the user agent. The recorder stores an explicit caveat alongside
identity:

> Identity is the account the server authenticated for this request; it is not
> proof of which person operated the client.

### Current limitations

- Detail-view investigation context (concurrent sessions, device first-seen,
  actor IPs within 24h) is computed per request with several queries; behaviour
  on a large `audit_events` table is **not established**.

---

## Engineering History

### Chronology

| Date | Change | Commit / migration |
|---|---|---|
| 2026-09-09 | Audit log repository, API and UI over `event_log` | `481e9a0` |
| 2026-09-15 | Audit logging expanded with detailed admin UI views | `52ce8c5` |
| 2026-09-18 | **Legacy audit logs replaced by operator audit and actor attribution** | `5b9d094` |
| 2026-09-18 | `audit_events` + `auth_sessions` created, append-only | migration `a7u8d9i0t1r2` |
| 2026-09-18 | Tiered search, three-band detail view; journal moved to System Monitor | `f51ed80` |
| 2026-09-18 | Compact, sectioned audit event detail drawer | `2a2d7d1` |

### From event_log to audit_events

The first audit implementation (2026-09-09, `481e9a0`) read `event_log` — the
machine event journal. That works for "what did the system do" and fails for
"who did this", because `event_log` rows have no authenticated actor.

The 2026-09-18 replacement (`5b9d094`) introduced a separate append-only table
with actor attribution, server-side sessions, and the intent-first recorder.
`event_log` remains, and remains appropriate, for machine events — the System
Event Journal moved to System Monitor (`f51ed80`) rather than being deleted.

The taxonomy preserves the seam honestly: `INVENTORY_FIX_ALIGN` is labelled
"Inventory fix (legacy, type not recorded)" and exists only for imported rows.
Legacy data was not retro-fitted with a fix type it never had, and `provenance`
lets a reader filter native from legacy.

---

# Case Study: Verification of the operator audit requirements

**Date:** 2026-09-21
**Feature:** Operator audit
**Type:** Investigation
**Severity:** N/A
**Status:** Complete — all requirements verified
**Related Components:** `audit_events`, `app/audit/*`, `app/api/routes/audit.py`

---

## 1. What Was Happening?

An eight-point operator-audit specification needed verification against the
implementation. The IB Gateway was unavailable, so live trading could not be
exercised.

## 2. Expected Behavior

All eight requirements satisfied and demonstrable.

## 3. Initial Understanding

The initial expectation was that verification would be blocked by the
unavailable gateway. It was not: the intent-first recorder means the audit trail
is exercised *most* strictly when the side effect fails, because the `PENDING`
row is committed before the broker is contacted.

A second initial assumption was wrong in a way worth recording: the local
database was assumed to be merely stale. In fact `audit_events` and
`auth_sessions` did not exist at all — see the Alembic case study in
[Chapter 2](../part1_understanding/02_database_and_persistence.md).

## 4. Symptoms / Evidence

Evidence gathered by exercising the running backend and reading `audit_events`
directly.

Identity capture, from a login with a forwarded IP:

```
action      | LOGIN_SUCCEEDED
category    | AUTHENTICATION
result      | SUCCEEDED
actor_type  | USER
actor_email | audit-test@zanrad.com
actor_role  | admin
client_ip   | 203.0.113.77
auth_method | password
http_method | POST
http_path   | /api/v1/auth/login
```

Settings changes recorded full before/after state, e.g.:

```
action      | ACCOUNT_SETTINGS_UPDATED
before      | {... "cancel_exposure": false ...}
after       | {... "cancel_exposure": true ...}
```

Emergency and system-control actions recorded outcomes including failures:

```
 EMERGENCY      | KILL_SWITCH_ENGINE_FLATTEN | ACCEPTED
 EMERGENCY      | COMPLETE_ACCOUNT_FLATTEN   | FAILED   | Could not connect to TWS/Gateway
 SYSTEM_CONTROL | SERVICE_START              | FAILED   | systemctl ... Access denied
```

Every row had `recorded_at` and `completed_at` set — intent recorded first, then
finalized.

## 5. Investigation

### Step 1
Read `taxonomy.py`. All nine categories present, including the six the
specification named.

### Step 2
Applied the migration to create the tables (Chapter 2 case study), then logged
in and exercised each category over HTTP.

### Step 3
Checked failure paths specifically. With the gateway down,
`COMPLETE_ACCOUNT_FLATTEN` recorded `FAILED` with the reason rather than
recording nothing — confirming intent-first durability.

### Step 4
Verified search filters server-side, and immutability at the database level
inside a rolled-back transaction.

## 6. Root Cause

Not applicable — investigation, not a defect.

**Confirmed findings:**

| # | Requirement | Evidence |
|---|---|---|
| 1 | User email, role, IP on all actions | Every row carries all three |
| 2 | Settings changes with previous/new values | Full `before_state`/`after_state` diffs |
| 3 | Inventory fix type recorded | `INVENTORY_FIX_ACTIONS` mapping; `test_inventory_fix_records_exact_fix_type_and_state` |
| 4 | Manual orders and Close Pair | `test_manual_order_submit_*`, `test_close_pair_is_audited_with_order_ids` |
| 5 | Kill Switch / Kill Manual / Complete Flatten | Recorded live, including the gateway-down failure |
| 6 | Service controls; audit committed before trading-backend restart | `operation(required=True)` + `SELF_UNIT`; 7 mocked tests |
| 7 | Logical categorization | Nine categories via `/audit/facets` |
| 8 | Advanced multi-criteria search | All filters verified server-side |

## 7. Code-Level Location

**Files:** `backend/app/audit/taxonomy.py`, `backend/app/audit/recorder.py`,
`backend/app/api/routes/audit.py`, `backend/app/api/routes/service_control.py`

## 8. The Fix

None required. The one change made was to the local database (migration), not
the code.

## 9. Verification

- Existing suites: 119 passed across the five `test_audit_*` files plus
  cancel-exposure and basket-retry.
- Live API: all categories exercised against the running backend.
- Immutability: both `UPDATE` and `DELETE` refused by trigger.
- UI: `frontend/e2e/auditTrail.spec.ts`, 5 tests.

## 10. Lessons Learned

- **Intent-first auditing is verified best when the side effect fails.** A
  broker outage is a good time to test an audit trail, not a bad one.
- Requirement 6's ordering guarantee is testable without touching `systemctl` —
  seven mocked tests cover it. Calling the real service-control endpoint to
  "check" it was unnecessary and touched the host's service manager; the mocked
  tests were the correct instrument.

## 11. Open Questions

- Behaviour of the detail-view investigation queries at scale is **not
  established** — the table held fewer than 20 rows during verification.
