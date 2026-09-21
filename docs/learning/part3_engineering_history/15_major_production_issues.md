# Chapter 15 — Major Production Issues

This chapter holds the case study template and the inventory of P0 findings.
Individual case studies live in the chapter for the feature they affect, so that
a reader working on the kill switch finds the kill-switch history without
hunting.

---

## Case study template

Copy this. Do not remove sections — write "Not established by available
material" instead. An empty section is information; a missing section is not.

```markdown
# Case Study: [Short Human Title]

**Date:** YYYY-MM-DD
**Feature:** [Feature/System]
**Type:** Bug / Feature / Design Change / Reliability Fix / Data Integrity Fix /
          Operational Fix / Security Fix / Architecture Change / Investigation /
          Documentation Correction
**Severity:** P0/P1/P2/P3/N/A
**Status:** Fixed / Partially Fixed / Monitoring / Superseded / Open
**Related Components:** [Classes/services/tables]

---

## 1. What Was Happening?
What was actually observed. Factual. What did the operator or developer see?

## 2. Expected Behavior
What should have happened.

## 3. Initial Understanding
What we believed at the time. **Do not rewrite using the final understanding.**
If the first theory was wrong, say so — that is the learning.

## 4. Symptoms / Evidence
Only evidence actually available: logs, database observations, exceptions, UI
behavior, broker callbacks, test failures, monitoring signals.

## 5. Investigation
### Step 1 — what was checked
### Step 2 — what was found
### Step 3 — what was ruled out
### Step 4 — what identified the root cause
Do not collapse a real sequence into a polished story.

## 6. Root Cause
Separate: confirmed root cause / contributing factor / unknown factor.
Do not overstate certainty.

## 7. Code-Level Location
**File:** `path/to/file.py`
**Class:** `ClassName`
**Function / Method:** `function_name()`

**Call path:**
```text
A()
  → B()
    → C()
      → problematic_function()
```

## 8. The Fix
What changed, and why that approach. Include what was tried and rejected.

## 9. Verification
How we know it works. Name the tests. If the regression test was confirmed to
fail without the fix, say so — that is much stronger evidence.

## 10. Lessons Learned
Transferable, not restatements of the fix.

## 11. Open Questions
What remains unknown. Be explicit.
```

### Section 8–11 note

The template sections beyond 7 were defined for this documentation set on
2026-09-21 and are not inherited from an earlier specification. They exist
because "what was tried and rejected" (8), "did the test actually fail without
the fix" (9), and "what is still unknown" (11) turned out to carry most of the
transferable value in the case studies written so far.

---

## P0 inventory — 2026-09-03 structured review

On 2026-09-03 a structured review produced three reports under `docs/review/`,
read against the commit state of that date. They remain the single largest body
of investigation material in the repository.

| Report | P0 | P1 | P2 | P3 |
|---|---|---|---|---|
| [`BUGS-concurrency.md`](../../review/BUGS-concurrency.md) | 3 | 4 | 6 | 2 |
| [`BUGS-lifecycle.md`](../../review/BUGS-lifecycle.md) | 4 | 10 | 7 | 4 |
| [`BUGS-resilience.md`](../../review/BUGS-resilience.md) | 3 | 9 | 8 | 0 |

The reports label each finding **certain**, **likely** or **speculative**. That
convention is preserved here and throughout this documentation set. A finding
marked "likely" in 2026-09-03 is still "likely" unless someone re-verified it.

### Concurrency P0s and their current status

| # | Finding | Status today |
|---|---|---|
| C1 | No unique constraint on `kill_switch_operations`; "strict idempotency" is check-then-act. Two square-offs both arm and both flatten — account flips from flat to an equal and opposite position. | **Fixed (Confirmed).** Partial unique index added (`i3j4k5l6m7n8`), later widened to `(account_id, scope)` (`l1m2n3o4p5q6`). |
| C2 | Close-pair, kill-switch flatten and critical recovery all call `BasketCoordinator.execute` directly, bypassing `_acquire_execution_claim`, each minting a random `uuid4()` so no key collides. Two independent MARKET reverses for one position. | **Unknown.** Not re-verified. |
| C3 | Kill switch armed *after* an OPEN passed the gate but before the basket submits. The gate is read once, ~6 awaits and up to ~15s before `placeOrder`. Kill switch reports COMPLETE while a new position sits open on a halted account. | **Unknown.** Not re-verified. |

### What the review confirmed was already correct

Recorded because it is as valuable as the findings, and because a future reader
may otherwise re-investigate settled ground:

- Job claiming is genuinely atomic — `FOR UPDATE SKIP LOCKED` in the same
  transaction as the `UPDATE ... SET status='CLAIMED'`.
- Terminal status writes and heartbeats are correctly fenced on `worker_id`;
  the takeover scenario was traced end to end and the fence holds.
- `execution_claims` is a real insert-or-retake in one statement, with the
  `ON CONFLICT` arm restricted to `ABANDONED`.
- `_exposure_guard` and `_reseed_model_value_used` use the same total ordering
  (`sorted(keys, key=repr)`), so they cannot deadlock against each other.

### Re-verification status

Of the 10 P0 findings, **two** have been re-verified against current code while
assembling this documentation (C1 fixed, C11 fixed — the latter a P2). The rest
carry status **Unknown** and are listed in
[Chapter 21](../part4_ownership/21_current_known_limitations.md).

This is itself a finding: a review of that quality deserves a tracked
remediation list, and none exists in the repository. Producing one is the single
highest-value follow-up available.

---

## Where the case studies are

| Case study | Chapter |
|---|---|
| Alembic branch drift | [2 — Database](../part1_understanding/02_database_and_persistence.md) |
| Test engine pooling across event loops | [2 — Database](../part1_understanding/02_database_and_persistence.md) |
| Heartbeat exception on a lost lease | [4 — Worker Execution](../part1_understanding/04_worker_execution.md) |
| Reject reason showed a numeric id | [5 — Account Routing](../part1_understanding/05_account_routing_and_strategy.md) |
| Execution tests depended on the wall clock | [6 — RMS](../part1_understanding/06_rms.md) |
| Red Zone gate had no test through the order path | [6 — RMS](../part1_understanding/06_rms.md) |
| Signal tray showed retries as extra legs | [7 — OMS](../part1_understanding/07_oms_and_basket_execution.md) |
| Manual kill-switch cache released only on COMPLETE | [10 — Kill Switch](../part2_safety_operations/10_kill_switch.md) |
| Compensation suppressed while kill switch armed | [10 — Kill Switch](../part2_safety_operations/10_kill_switch.md) |
| Positions lingered after flatten | [10 — Kill Switch](../part2_safety_operations/10_kill_switch.md) |
| Operator audit verification | [12 — Audit](../part2_safety_operations/12_operator_audit.md) |
| Rogue tests asserted on a removed path | [13 — Notifications](../part2_safety_operations/13_notifications.md) |
| Flapping test raced its notification | [13 — Notifications](../part2_safety_operations/13_notifications.md) |
| Leaked dependency override poisoned later tests | [16 — Reliability](16_reliability_fixes.md) |
