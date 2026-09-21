# Chapter 6 — RMS

## Current Implementation

### Responsibility

Pre-trade risk. Decide whether an intent may reach the broker. RMS check
classes are pure: they evaluate against an `RMSContext` and must not mutate the
database (`AGENTS.md` §10).

### Major components

| Component | File |
|---|---|
| `RMSEngine` | `backend/app/rms/engine.py` |
| Check classes | `backend/app/rms/checks/` |
| Models / context | `backend/app/rms/models.py` |
| Cancel Exposure | `backend/app/services/cancel_exposure.py` |
| Session clock | `backend/app/services/session_clock.py` |

### Check sequence

`AGENTS.md` §6.5 requires checks **1, 2, 3, 4, 7, 8, 101** to run sequentially
before every engine order. The engine must not be bypassed on any path.

### The Red Zone gate

`SessionClock` blocks non-emergency execution when the *projected* execution
time falls in a restricted window. `projected_in_red_zone(now)` adds
`gateway_max_wait_sec` to now and asks `in_red_zone()`, which is true when:

- before the RTH open for that date
- within `post_open_delay_seconds` after the open (default 120s)
- within `buffer_seconds` of the close (default 45s)
- all day on a non-trading day

Defaults: `red_zone_buffer_seconds=45`, `post_open_delay_seconds=120`,
`ibkr_gateway_max_wait_sec=8.0`.

The gate runs in `OrderManager.process_signal_execution` **before** account
fan-out and before the execution claim, and returns a result with
`deferred_red_zone=True`. Deferred signals are released later by
`app/services/red_zone_release.py`.

This is a DO-NOT-BYPASS gate (`AGENTS.md` §6.7).

### Cancel Exposure

A per-account setting (`accounts.cancel_exposure`, default **off**, which is the
safe default).

When **off**, an incoming pair signal may not partially cancel an existing
paired exposure. From the module docstring of
`backend/app/services/cancel_exposure.py`:

```text
existing: AAPL BUY + EWC SELL
incoming: AAPL SELL + XYZ BUY   → AAPL leg closes, XYZ does not close EWC → REJECT
incoming: AAPL SELL + EWC BUY   → both legs close the same pair          → ALLOW
incoming: AAPL BUY  + XYZ SELL  → AAPL same direction, not closing       → ALLOW
```

Only OPEN intents with two legs are subject to the check. CLOSE intents are
explicit closes by `trade_id` and are never rejected here.

When **on**, partial cancels are permitted, and the per-symbol money limit
switches to a **net** basis rather than gross (`38bc676`, 2026-09-18), with the
net basis tracking signed quantity valued at order price (`a9c6273`, same day).

### Failure behavior

Fail closed. `abd604e` (2026-09-02) made the symbol and position checks
fail-closed explicitly and removed a permissive default.

### Current limitations

- **Finding C13 (P1, "likely", 2026-09-03):** `exposure_key` uses the raw leg
  symbol, which is stripped but never upper-cased, while reconcile and flatten
  paths normalise. Two alerts differing only in case would take different
  exposure locks and read different budget buckets, silently doubling the
  money-per-symbol ceiling. **Status: not re-verified. Unknown.**
- **Finding C5 (P1, "certain"):** cross-thread read-modify-write on
  `RMSContext.margin_commitments` — the TWS thread can replace the dict between
  the loop's `setdefault` and its `.append`, losing a commitment and
  over-reporting headroom. **Status: not re-verified. Unknown.**

---

## Engineering History

### Chronology

| Date | Change | Commit |
|---|---|---|
| 2026-08-14 | RMS introduced alongside OMS | `902ca4d` |
| 2026-09-02 | Strict risk ceiling; fail-closed symbol and position checks | `abd604e` |
| 2026-09-03 | Concurrency review flags C5 and C13 against RMS state | `448a9e2` |
| 2026-09-08 | **Global Red Zone gate + post-session release** | `4741de4` |
| 2026-09-09 | NYSE holiday calendar and startup guards | `a82cbca` |
| 2026-09-18 | Per-symbol limit uses net basis when Cancel Exposure is on | `38bc676` |
| 2026-09-18 | Net per-symbol basis tracks signed quantity at order price | `a9c6273` |
| 2026-09-21 | Red Zone gate behaviour pinned by tests for the first time | `64f3009` |

### On the Red Zone gate

The gate arrived on 2026-09-08 (`4741de4`) together with its release mechanism,
which is notable: the deferral and the un-deferral were designed as one unit. A
gate that parks work without a release path is an outage.

The holiday calendar (`a82cbca`, 2026-09-09) closed the remaining hole — before
it, "is this a trading day" was not something the clock could answer correctly.

Until 2026-09-21 the gate had no test asserting that `OrderManager` actually
defers. `test_red_zone.py` covered `SessionClock`'s arithmetic thoroughly with
explicit datetimes, but nothing exercised the gate through the order path. That
gap is the subject of the case study below.

---

# Case Study: Execution tests depended on the wall clock

**Date:** 2026-09-21
**Feature:** RMS / Red Zone gate — test correctness
**Type:** Reliability Fix (test)
**Severity:** P2
**Status:** Fixed
**Related Components:** `SessionClock`, `OrderManager`, `ExecutionWorkerPool`, `backend/tests/conftest.py`

---

## 1. What Was Happening?

37 tests across 9 files failed. The affected files were the execution-path
suites: multi-account routing, TradingView execution integration, production
path hardening, order manager, n-leg execution, execution audit persistence,
Model Blue persistence, hardening lifecycle, worker recovery classification.

## 2. Expected Behavior

The suite passes regardless of when it runs.

## 3. Initial Understanding

The initial hypothesis was that these were *product* defects introduced or
exposed by recent work, since 47 tests were failing in total and several touched
recently-modified areas. That was wrong. It was also initially assumed all 47
shared one cause; they did not — they split into four unrelated causes, of
which this was the largest.

## 4. Symptoms / Evidence

Every failure in this group carried the same log line:

```
WARNING app.services.order_manager:order_manager.py:875
  RED_ZONE_BLOCKED signal MBG-ACC-1 deferred (projected red zone) buffer=45
```

and the same assertion shape:

```
assert 0 == 1
 +  where 0 = len([])
 +    where [] = FanoutExecutionResult(outcomes=[], had_unexpected_error=False,
                                       deferred_red_zone=True).outcomes
```

## 5. Investigation

### Step 1
Grouped the 47 failures by file and counted `RED_ZONE_BLOCKED` occurrences per
file. Eight files mentioned it; four did not.

### Step 2
Read `in_red_zone()` and `projected_in_red_zone()`, then queried the live clock:

```
now ET               : 2026-09-21 07:01:57-04:00
weekday              : Monday
in_red_zone          : True
projected_in_red_zone: True
buffer_seconds       : 45
post_open_delay_s    : 120
```

07:01 ET is before the 09:30 open, so the gate was correctly closed. **The
system was behaving exactly as designed.**

### Step 3
Ruled out a product defect. Confirmed these tests would pass between roughly
09:32 and 15:59 ET on a trading day and fail at every other time — overnight,
at weekends, and on holidays.

### Step 4
Found the existing convention for neutralising the gate in a test that already
passed reliably: `backend/tests/test_reject_reason_account_scope.py` patches
`app.services.session_clock.get_session_clock` with a mock whose
`projected_in_red_zone` returns `False`. Eight files needed the same treatment.

## 6. Root Cause

**Confirmed root cause:** the tests asserted that orders were routed without
controlling the session clock, so their outcome depended on the wall-clock time
at which the suite ran.

**Not a product defect.** The gate was correct throughout.

## 7. Code-Level Location

**File:** `backend/app/services/order_manager.py`
**Class:** `OrderManager`
**Function:** `process_signal_execution()`

**Call path:**

```text
OrderManager.process_signal_execution()
  → get_session_clock()
    → SessionClock.projected_in_red_zone(now)
      → in_red_zone(now + gateway_max_wait_sec)
        → True before RTH open
          → return FanoutExecutionResult(outcomes=[], deferred_red_zone=True)
             [test asserted outcomes was non-empty]
```

## 8. The Fix

An autouse fixture in `backend/tests/conftest.py`, placed alongside the existing
`_clear_kill_switch_cache` / `_restore_trading_app_state` fixtures that follow
the same pattern:

```python
@pytest.fixture(autouse=True)
def _neutralize_red_zone(request):
    if request.node.get_closest_marker("real_session_clock"):
        yield
        return
    ...
    def _open_session_clock():
        clock = real_factory()          # a real SessionClock
        clock.in_red_zone = lambda now: False
        clock.projected_in_red_zone = lambda now: False
        return clock
    with patch.object(session_clock_module, "get_session_clock", _open_session_clock):
        yield
```

A real `SessionClock` is returned so `buffer_seconds`, `resolved_session_close`
and the rest keep production values; only the two gating predicates are forced
open. The `real_session_clock` marker is registered in `pyproject.toml`.

## 9. Verification

- All 8 files went fully green.
- `test_red_zone.py` (28 tests), `test_risk_exit_monitor.py`,
  `test_startup_guard.py`, `test_nyse_calendar.py` and `test_system_events_api.py`
  all still pass — they construct `SessionClock` directly with explicit
  datetimes and never call the patched factory, so the fixture cannot mask them.

## 10. Lessons Learned

- **A failing test is evidence about the test as often as about the system.**
  The first instinct was to look for a product regression; the system was right
  and the tests were wrong, every time, in all four root causes found that day.
- A test that depends on the wall clock is not flaky — it is *deterministically
  wrong outside a specific window*, which is worse, because it looks fine all
  afternoon and fails the moment someone runs it in the evening.
- Suppressing a safety gate to make tests pass creates a coverage hole. That is
  why the gate's behaviour was pinned explicitly in the same change (below)
  rather than left implicit.

## 11. Open Questions

- Why these tests were originally written without clock control is **Unknown**.
  Many predate the Red Zone gate (2026-09-08), so the most likely explanation is
  that the gate was added beneath tests that already existed and nobody re-ran
  the suite outside market hours. This is **plausible but not established**.

---

# Case Study: The Red Zone gate had no test through the order path

**Date:** 2026-09-21
**Feature:** RMS / Red Zone gate
**Type:** Test Coverage
**Severity:** N/A
**Status:** Fixed
**Related Components:** `SessionClock`, `OrderManager`

---

## 1. What Was Happening?

`SessionClock` is a DO-NOT-BYPASS gate, but no test asserted that `OrderManager`
actually refuses to fan a signal out while the projected red zone is open.
`test_red_zone.py` covered only the clock's own arithmetic.

## 2. Expected Behavior

A safety gate listed in `AGENTS.md` §6 should have a test proving the gate
gates, not merely that the predicate computes.

## 3. Initial Understanding

The gap was not known until the `_neutralize_red_zone` fixture was written. The
question "does this fixture hide anything that was previously covered?" surfaced
it.

## 4. Symptoms / Evidence

```
$ grep -rn "deferred_red_zone is True\|RED_ZONE_DEFERRED" tests/
(no matches)
```

No test asserted deferral. `test_red_zone.py` calls
`SessionClock(...)` directly with explicit datetimes and never goes through
`get_session_clock()` or `OrderManager`.

## 5. Investigation

### Step 1
Searched the suite for any assertion on `deferred_red_zone` or
`RED_ZONE_DEFERRED`. None.

### Step 2
Confirmed `test_red_zone.py` builds clocks directly, so the new fixture could
not affect it either way.

### Step 3
Concluded the fixture was safe *and* that it would permanently mask the gate if
the gap were left unfilled.

## 6. Root Cause

**Confirmed:** coverage gap. The gate was added on 2026-09-08 with unit tests
for the clock but no integration test through `OrderManager`.

## 7. Code-Level Location

**File:** `backend/tests/test_red_zone.py` (tests added here)
**Covers:** `backend/app/services/order_manager.py` → `process_signal_execution()`

## 8. The Fix

Two tests marked `@pytest.mark.real_session_clock` so they opt out of the
neutralising fixture and control the clock themselves:

- `test_order_manager_defers_signal_inside_projected_red_zone` — asserts
  `deferred_red_zone is True`, `outcomes == []`, and that the inner execution
  path was **not** awaited.
- `test_order_manager_fans_out_when_red_zone_is_clear` — the control. Asserts
  the inner execution path *was* awaited.

The control asserts on the execution entrypoint rather than the return value,
because with the gate open the return value belongs to the patched inner call.

## 9. Verification

`test_red_zone.py` 30 passed (was 28). Full suite green.

## 10. Lessons Learned

- When you disable something globally in test setup, that is the moment to ask
  what coverage you just removed — and to replace it before moving on.
- "The predicate is tested" and "the gate is tested" are different claims. Unit
  tests on a pure function say nothing about whether anyone calls it.

## 11. Open Questions

None.
