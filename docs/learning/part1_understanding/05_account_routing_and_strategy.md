# Chapter 5 — Account Routing & Strategy

## Current Implementation

### Responsibility

One inbound signal becomes N independent execution attempts, one per enabled
account. Each account sizes the trade independently and passes or fails risk
independently.

### Major components

| Component | File | Role |
|---|---|---|
| Account router | `backend/app/accounts/router.py` | Resolves enabled accounts and their context |
| Account context | `backend/app/accounts/context.py` | Per-account settings including `cancel_exposure` |
| Config service | `backend/app/accounts/config_service.py` | Account settings persistence |
| Position sizer | `backend/app/services/model_blue/` | Model Blue sizing |
| Order manager | `backend/app/services/order_manager.py` | Fan-out orchestration |

### Fan-out semantics

The invariants, all covered by `backend/tests/test_multi_account_routing.py`:

- A disabled account is not in the router's output at all.
- An RMS rejection in account A does not prevent account B from executing.
- Per-symbol limits are per account, not global.
- Two accounts trading the same symbol produce independent intents with
  independent exposure keys.

The result type is `FanoutExecutionResult(outcomes, had_unexpected_error,
deferred_red_zone)`. An empty `outcomes` list with `deferred_red_zone=True`
means the Red Zone gate parked the signal before any account was considered —
the gate runs **before** fan-out and before the execution claim.

### Database tables

`accounts`, `allocations`, `strategies`, `per_symbol_limits`.

### Failure behavior

Per-account isolation is the point. A failure attributable to one account is
tagged with that account's `ibkr_account` code in the reject reason, so an
operator can tell which account failed and why.

### Current limitations

- All accounts share one TWS socket and one rate limiter, so isolation is
  logical, not physical. A flood on one account can cause
  `GatewayPacingTimeout` on another (Risk 2,
  [`18-KNOWN-RISKS.md`](../../engineering/18-KNOWN-RISKS.md)).

---

## Engineering History

### Chronology

| Date | Change | Commit |
|---|---|---|
| 2026-08-18 | Account strategy routing and allocation validation | `2c63b3b` |
| 2026-08-19 | Model Blue position sizer | `d9a9119`, `1e8b1d5` |
| 2026-08-22 | Add-account cache clearing | `22c9de3` |
| 2026-08-31 | Cross-account data isolation with RBAC | `28f5325` |
| 2026-09-03 | **Major fixes in position sizing** | `448a9e2` |
| 2026-09-18 | Exception reject reasons tagged with the correct account | `c943f87` |

---

# Case Study: Reject reasons showed a numeric id instead of the IBKR account

**Date:** 2026-09-18
**Feature:** Account routing
**Type:** Data Integrity Fix
**Severity:** P2
**Status:** Fixed
**Related Components:** `OrderManager`, `accounts.id`, `signals.reject_reason`

---

## 1. What Was Happening?

On the exception path, a signal's `reject_reason` fell back to a bare numeric
account id rather than the IBKR account code, and a traceback was logged on
every failed execution.

**Established by:** the module docstring of
`backend/tests/test_reject_reason_account_scope.py`, which states the defect
directly, and commit `c943f87` (2026-09-18),
"fix(order_manager): compare accounts.id as int when tagging exception reject
reasons".

## 2. Expected Behavior

A failure in account `DU12345` should produce
`Account DU12345: <reason>`, and should not log a resolution traceback.

## 3. Initial Understanding

Not separately recorded. The test docstring presents the diagnosis as final.

## 4. Symptoms / Evidence

From the test docstring:

> `accounts.id` is BIGINT and `account_scope` arrives as a string (ingest job
> scope). Comparing the column to a `str` made PostgreSQL raise
> "operator does not exist: bigint = character varying", so the reason fell back
> to the bare numeric id and a traceback was logged on every failed execution.

## 5. Investigation

The recorded sequence is: the reject reason was wrong → the account lookup was
failing → the lookup failed because of a type mismatch in the SQL comparison.
Intermediate steps are **not established**.

## 6. Root Cause

**Confirmed root cause:** `account_scope` arrives as a string because it
originates from the ingest job, while `accounts.id` is `BIGINT`. PostgreSQL has
no `bigint = character varying` operator, so the query raised, the lookup was
swallowed, and the code fell back to the numeric id.

## 7. Code-Level Location

**File:** `backend/app/services/order_manager.py`
**Class:** `OrderManager`
**Function:** `process_signal_execution()` — exception path

**Call path:**

```text
OrderManager.process_signal_execution(signal, account_scope="202")
  → exception raised in inner execution
    → resolve account for reject reason
      → SELECT ... WHERE accounts.id = '202'   [bigint = varchar → raises]
        → lookup swallowed, falls back to numeric id   [defect]
```

## 8. The Fix

Compare `accounts.id` as an integer when tagging exception reject reasons
(`c943f87`). Locked in by
`backend/tests/test_reject_reason_account_scope.py`, which asserts both that the
reason contains the IBKR code and that no
`"Failed resolving account_scope"` record is logged.

## 9. Verification

The regression test passes at the current commit.

## 10. Lessons Learned

- Identifier *scope* and identifier *type* are both load-bearing in this system.
  `AGENTS.md` §9 lists them for exactly this reason: `account_id` is a BIGINT
  surrogate, `ibkr_account` is a string code, and they are not interchangeable.
- A swallowed exception on an error path produced a wrong but plausible value.
  Degrading to a fallback silently is how a type bug survives to production;
  the traceback was being logged, but nothing failed loudly.

## 11. Open Questions

- Whether any operator was actually misled by the numeric id before the fix is
  **Unknown**.
