# Chapter 13 — Notifications

## Current Implementation

### Responsibility

One pipeline for every operator-facing alert, across Telegram and the in-app
Notification Center, with suppression logic so that a cascade of related events
does not become a cascade of messages.

### Components

| Component | Location |
|---|---|
| Orchestrator | `backend/app/services/notification/` |
| Intelligence layer | `backend/app/services/notification/intelligence.py` |
| Channels | `backend/app/services/notification/channels/` |
| Canonical messages | `backend/app/services/notification_canonical.py` |
| Tables | `notification_log`, `notification_deliveries`, `user_notification_state`, `user_notification_reads` |

### The single-pipeline rule

Producers emit a `NormalizedEvent` to `orchestrator.ingest_event()`. They do not
call a channel directly.

`send_canonical_telegram` still exists in `position_reconciler.py` but is
**deprecated and never called**. Its import carries the comment:

```python
send_canonical_telegram,  # noqa: F401 — deprecated, kept for test patch compatibility
```

and at the emission site:

```python
# Centralized notification only — legacy send_canonical_telegram removed (single system)
```

A component constructed **without** an orchestrator therefore raises no alert at
all. This is a real trap — it caused three stale tests to assert on a mock that
could never be called. See the case study.

### Flapping detection

`intelligence.py` keeps an in-memory sliding window per scope:

- `notification_flapping_window_sec` — default **300.0**
- `notification_flapping_threshold` — default **3**

Once oscillations fall below the threshold the alerted flag resets.

### Dispatch style

The reconciler dispatches through `asyncio.create_task(...)` — fire and forget.
Tests must yield to the loop before asserting; see the flapping case study.

### Failure behavior

Suppression is layered: cooldowns, hourly volume limits, cascade suppression
during automated restarts, and debounce on service stop/start. `STARTUP_AGGREGATION`
is explicitly exempted from both cooldown and hourly volume suppression
(`6867289`, `3e61225`) because suppressing the startup summary defeats its
purpose.

### Current limitations

- The flapping window is in-memory per process and does not survive a restart.
- Suppression tuning is empirical; the September 17 commit sequence shows it was
  arrived at by iteration against real behaviour, not derived.

---

## Engineering History

### Chronology

| Date | Change | Commit |
|---|---|---|
| 2026-08-31 | Telegram notification semantics and state machine fixes | `00b7520`, `4b88cb2` |
| 2026-09-08 | Telegram notifications for service status and market-closed alerts | `9b26974` |
| 2026-09-09 | Notification Center, canonical message dictionary, durable read state | `05263f2` |
| 2026-09-15 | Rogue trade spam fix | `460637e` |
| 2026-09-17 | **Centralized notification system with multi-channel support and intelligence layer** | `42a36cd` |
| 2026-09-17 | Service lifecycle watcher + systemd hooks for all four services | `da0cbe7`, `3030f7d` |
| 2026-09-17 | Cascade suppression, debounce, startup aggregation exemptions | `2982349`, `f8f83d2`, `6867289`, `3e61225` |
| 2026-09-18 | Frontend overhaul: eliminate toast replay spam | `9e469cc` |
| 2026-09-18 | Atomic frontend/Telegram sync via `event_log` mirror | `6b3f707` |
| 2026-09-21 | Flapping test made deterministic | `64f3009` |

### Two weeks of alert noise

The September 17 sequence — roughly fifteen commits in one day — is the clearest
example in the repository of a problem that could only be solved empirically.
Read in order, the titles trace a single argument:

1. Events are emitted per-component → spam during a restart.
2. Suppress cascades during automated restarts (`2982349`).
3. But distinct actions must still alert → debounce rather than suppress
   (`f8f83d2`).
4. But a restart looks like a stop then a start → poll for re-activation before
   alerting on a stop (`e3e01b4`, then up to 10s in `1d3895c`).
5. But the startup summary is now suppressed too → exempt
   `STARTUP_AGGREGATION` (`6867289`, `3e61225`).
6. And the state reported must be *probed*, not assumed (`42df0bb`, `ae067f7`,
   `e402f91`).

Step 6 is the durable lesson. Several fixes that day were about replacing
*inferred* state with *evidence*: probing port 4002 for `ib_login` rather than
assuming, using `systemctl list-jobs` to detect a restart rather than guessing
from a stop event, requiring `broker_connection` ready before claiming broker
recovery (`5b35655`), and requiring an active unrecovered `BROKER_LOST`
incident before emitting `BROKER_RECONNECTED` (`e5d1f74`).

The naming work (`ae067f7` "evidence-based state and canonical terminology",
`5033222` "semantic lifecycle headings") belongs to the same idea: an operator
cannot act on an alert that says something the system does not actually know.

---

# Case Study: Rogue-trade tests asserted on a removed code path

**Date:** 2026-09-21
**Feature:** Notifications × reconciliation
**Type:** Reliability Fix (test)
**Severity:** P2
**Status:** Fixed
**Related Components:** `PositionReconciler`, notification orchestrator

---

## 1. What Was Happening?

Three tests across `test_manual_trading_m1d.py` and
`test_phase2_rogue_audit_csv.py` failed. All asserted that
`send_canonical_telegram` had been called when a rogue trade was detected.

## 2. Expected Behavior

Detecting an untracked broker position should raise an operator alert.

## 3. Initial Understanding

Initially read as a product regression in rogue detection — plausibly caused by
recent reconciliation work.

That was wrong. Detection was working correctly throughout. The logs showed the
reconciler finding the orphan on every run:

```
Position reconcile run_id=310 broker_lines=2 match=1 ghost=131 orphan=1 ...
```

The `ROGUE_TRADE_DETECTED` rows were written to `event_log` as expected. Only
the alert assertion failed.

## 4. Symptoms / Evidence

```
assert any("TSLA" in str(c) and acc.ibkr_account in str(c) for c in mock_tg.call_args_list)
E   assert False
```

and

```
assert len(acc_calls) == 3
E   assert 0 == 3
E    +  where 0 = len([])
```

Zero calls, not wrong calls.

## 5. Investigation

### Step 1
Confirmed detection worked: `orphan=1` in the reconcile log and
`ROGUE_TRADE_DETECTED` present in `event_log`.

### Step 2
Grepped `position_reconciler.py` for the alert path and found the answer in two
comments — the import marked "deprecated, kept for test patch compatibility",
and the emission site marked "legacy `send_canonical_telegram` removed (single
system)".

### Step 3
Read the current emission path: `self._orchestrator.ingest_event(NormalizedEvent(...))`,
guarded by `if self._orchestrator is not None`.

### Step 4
Checked how the tests built the reconciler:

```python
reconciler = PositionReconciler(
    session_factory, client, interval_sec=9999.0, rogue_confirm_sweeps=1
)
```

No `notification_orchestrator`. The reconciler therefore could not raise an
alert by any route, and the patched mock could never be called.

## 6. Root Cause

**Confirmed root cause:** notification delivery moved to the centralized
orchestrator (`42a36cd`, 2026-09-17). `send_canonical_telegram` was retained as
an importable symbol so existing `patch()` calls would not fail with
`AttributeError` — but a patch that succeeds against a function nobody calls
produces a test that asserts nothing and then fails.

**Contributing factor:** keeping the symbol for patch compatibility made the
tests keep *running* instead of failing loudly at import, which delayed
discovery.

## 7. Code-Level Location

**File:** `backend/app/services/position_reconciler.py`
**Class:** `PositionReconciler`
**Function:** rogue detection/resolution inside the reconcile sweep

**Call path:**

```text
PositionReconciler.run_once()
  → classify diffs → confirmed rogue
    → event_repo.append(kind="ROGUE_TRADE_DETECTED")      # works
    → if self._orchestrator is not None:                  # None in tests
        asyncio.create_task(orchestrator.ingest_event(...))
      (no call to send_canonical_telegram anywhere)        [tests patched this]
```

## 8. The Fix

Tests now inject an orchestrator and assert on what the system actually does:

- `notification_orchestrator=orchestrator` passed to the reconciler
- assertions on `ingest_event` receiving a `NormalizedEvent` with the expected
  `event_type` and `details["symbol"]`
- plus assertions on the `ROGUE_TRADE_DETECTED` / `ROGUE_TRADE_RESOLVED`
  `event_log` rows

Dedup semantics were preserved exactly: 3 alerts on first detection, 0 on an
unchanged second sweep, 3 on resolution.

## 9. Verification

`test_manual_trading_m1d.py` 9 passed; `test_phase2_rogue_audit_csv.py` 8
passed.

## 10. Lessons Learned

- **Keeping a dead symbol alive "for test compatibility" is a trap.** Deleting
  it would have broken the patches loudly at the moment of the migration, when
  the context was fresh. Instead the tests silently stopped testing anything and
  failed later for reasons that looked like a product regression.
- When migrating a delivery mechanism, migrate its tests in the same change. A
  test asserting on the old path is not neutral; it is actively misleading.
- Detection and delivery are separate concerns and should be asserted
  separately. Here detection was fine the whole time.

## 11. Open Questions

- Whether other tests still patch deprecated symbols is **not established**. A
  sweep for `patch(` against symbols marked deprecated has not been done.

---

# Case Study: Flapping test raced its own notification

**Date:** 2026-09-21
**Feature:** Notifications — flapping detection
**Type:** Reliability Fix (test)
**Severity:** P3
**Status:** Fixed
**Related Components:** `BrokerNotificationListener`, `NotificationOrchestrator`, `notification_log`

---

## 1. What Was Happening?

`test_genuine_broker_oscillation_triggers_flapping` failed roughly one run in
three — **in complete isolation**, with no other tests running.

## 2. Expected Behavior

Three disconnect/reconnect cycles produce a flapping alert, deterministically.

## 3. Initial Understanding

First suspected as fallout from concurrent kill-switch work in the same tree,
since it appeared in a run immediately after those files changed. Ruled out by
reproducing it in isolation at a 1-in-3 rate with nothing else running.

## 4. Symptoms / Evidence

```
assert any("FLAPPING" in t for t in titles)
E   assert False
```

Test body: three `on_connection_closed()` / `on_connection_restored()` cycles
separated by `await asyncio.sleep(0.05)`, then a direct query of
`notification_log`.

## 5. Investigation

### Step 1
Ran the test 3× in isolation: pass, fail, pass. Not order-dependent.

### Step 2
Ran the whole file: 14 passed. Not a file-level interaction.

### Step 3
Read the test. The listener persists notifications from background tasks; the
final assertion queried the database after a fixed 50 ms sleep, with no
guarantee the write had landed.

### Step 4
Checked whether waiting longer could change the *verdict* rather than just the
timing. `notification_flapping_window_sec` defaults to **300 seconds** — four
orders of magnitude wider than the test's runtime — so polling for several
seconds cannot push the cycles outside the window. Waiting is safe.

## 6. Root Cause

**Confirmed root cause:** the test raced the asynchronous persistence of the
notification it was asserting on. A fixed sleep was used where a condition was
needed.

**Not a product defect.** Flapping detection behaved correctly.

## 7. Code-Level Location

**File:** `backend/tests/test_notification_restoration.py`
**Covers:** `BrokerNotificationListener` → `NotificationOrchestrator` →
`notification_log`

## 8. The Fix

Replaced the final fixed sleep with a bounded poll helper that returns as soon
as the predicate holds, or on a 5s deadline — returning the last titles seen
either way, so a genuine failure reports what actually arrived rather than an
opaque timeout.

## 9. Verification

12 consecutive isolated passes (previously ~1-in-3 failure), whole file 14
passed, full suite green.

## 10. Lessons Learned

- **`sleep(x)` is a guess; polling for a condition is an assertion.** Any test
  that waits on asynchronous persistence should wait for the outcome.
- Before lengthening a wait, check whether the extra time can change the result.
  Here the 300s window made it provably safe; with a 200ms window it would not
  have been.
- A test failing 1-in-3 *in isolation* rules out ordering and pollution
  immediately. Reproducing in isolation first saved bisecting the suite.

## 11. Open Questions

- Whether other tests in this file use the same fixed-sleep pattern is **not
  established**; only the failing one was changed.
