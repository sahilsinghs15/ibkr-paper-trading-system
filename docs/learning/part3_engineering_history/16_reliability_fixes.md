# Chapter 16 — Reliability Fixes

Changes that made the system keep working under failure, restart or load.

## Inventory

| Date | Change | Commit | Area |
|---|---|---|---|
| 2026-08-21 | Durable webhook queuing; non-blocking disk logging | `4c47e37` | Ingest |
| 2026-08-31 | Independent watchdog service | `8cbe353` | Monitoring |
| 2026-08-31 | Filesystem write errors handled in logging | `22bb0d4` | Logging |
| 2026-09-01 | Watchdog safety-gate flapping; health vs safety separated | `f8a9a91` | Watchdog |
| 2026-09-01 | False readiness degradation; transient timeout debounce | `423f65a` | Watchdog |
| 2026-09-01 | Recovery attempt tracking accuracy | `c4b626a` | Recovery |
| 2026-09-02 | Market-hours aware service lifecycle policy | `6f31919` | Lifecycle |
| 2026-09-03 | Heartbeat exception now sets `lease_lost` (finding C11) | — | Workers |
| 2026-09-16 | Kill-switch eventual convergence retries; row-level locking | `29fce78` | Kill switch |
| 2026-09-21 | Red Zone test determinism; leaked dependency override | `64f3009` | Test infra |

## Recurring pattern

Most reliability work here is one idea: **distinguish "failed" from "unknown"**.

- A heartbeat that *throws* proves nothing about lease ownership → treat as lost
  (Chapter 4).
- A watchdog probe returning 401 is an auth failure, not a safety verdict
  (`94ce7b6`).
- A service that stopped might be restarting → poll before alerting
  (`e3e01b4`, `1d3895c`).
- A missing broker probe is not "broker down" → probe port 4002 explicitly
  (`e402f91`).

In each case the original code treated an inconclusive result as a conclusive
one. The fix is always to make the inconclusive case explicit and then decide
deliberately what it should mean — which is usually "fail closed" on the trading
path and "stay quiet" on the alerting path.

---

# Case Study: A leaked dependency override poisoned later test files

**Date:** 2026-09-21
**Feature:** Test infrastructure
**Type:** Reliability Fix (test)
**Severity:** P2
**Status:** Fixed
**Related Components:** `backend/tests/conftest.py`, `app.dependency_overrides`, `get_db_session`

---

## 1. What Was Happening?

Ten tests across three files failed in a full suite run but passed when their
files were run alone:

- `test_position_exit_thresholds.py` (5)
- `test_reconcile_api.py` (4)
- `test_trading_pause_api.py` (1)

## 2. Expected Behavior

Test outcome should not depend on what ran before.

## 3. Initial Understanding

The first theory was connection pooling — the error mentioned asyncpg and the
repository already had precedent for pooling problems under test
(`4f945ca`, 2026-08-20, which introduced `NullPool` for tests). Applying
`NullPool` to the shared `session_factory` fixture did fix all ten.

**That theory was wrong, and the fix was reverted.** `NullPool` broke eight
different tests that had been passing — in `test_notification_restoration.py`,
both burst-stress files, and `test_manual_trading_fixes.py` — all of which pass
with a pooled engine and fail without one. Trading ten failures for eight
different failures is not progress.

This is the most instructive part of the episode: the first fix *worked* on the
tests being looked at, which is exactly why it was convincing.

## 4. Symptoms / Evidence

Initially:

```
asyncpg.exceptions._base.InterfaceError:
  cannot perform operation: another operation is in progress
```

After reverting `NullPool` and looking harder, the more informative error:

```
RuntimeError: Task <Task pending name='anyio.from_thread.BlockingPortal._call_func' ...>
  got Future <Future pending> attached to a different loop
```

logged as `Unhandled server error processing request:
http://testserver/api/v1/config/accounts/4975/positions/MBG-EXIT-.../exits`.

## 5. Investigation

### Step 1
Confirmed the three files pass together in isolation — so the polluter was
elsewhere in the suite.

### Step 2
Extracted collection order (`pytest --collect-only`, 149 files) and reproduced
the failure with files 1–107.

### Step 3
Bisected: 54–106 reproduced; 81–106 did not; 54–80 did; 68–80 did; 54–67 did
not; then file-by-file across 74–80.

Result: `tests/test_manual_trading_m1e.py` alone poisons
`test_position_exit_thresholds.py`.

### Step 4
Read `test_manual_trading_m1e.py`. Every test does:

```python
app.dependency_overrides.clear()          # at the START of each test
...
async def _override_get_db():
    async with session_factory() as session:
        yield session
app.dependency_overrides[get_db_session] = _override_get_db
```

The override closes over that test's `session_factory`, which is bound to that
test's event loop. It is cleared at the *start* of each test, so the **last**
test in the file leaves it installed. The next file to use `TestClient(app)` —
which runs the app on a different loop in its own thread — receives sessions
from a dead engine.

## 6. Root Cause

**Confirmed root cause:** a FastAPI dependency override outliving the test that
installed it, holding a closure over a loop-bound session factory.

**Confirmed non-cause:** connection pooling. `NullPool` masked the symptom by
forcing fresh connections; it did not address the leaked override, and it broke
eight tests that rely on pooling.

**Contributing factor:** the cleanup idiom was `clear()` at the start of each
test rather than teardown after it — which works for every test except the last
one in a file, making the leak invisible within the file.

## 7. Code-Level Location

**File:** `backend/tests/test_manual_trading_m1e.py` (source of the leak)
**Affected:** any later file using `TestClient(app)`
**Fix location:** `backend/tests/conftest.py`

**Call path:**

```text
test_manual_trading_m1e.py (last test)
  → app.dependency_overrides[get_db_session] = closure over loop-A session_factory
    → test ends; override never removed
      → test_position_exit_thresholds.py
        → TestClient(app)  (loop B, separate thread)
          → get_db_session → leaked closure → engine bound to dead loop A
            → RuntimeError: Future attached to a different loop
```

## 8. The Fix

An autouse fixture in `conftest.py`, following the existing
`_restore_trading_app_state` pattern, which saves and restores overrides around
**every** test:

```python
@pytest.fixture(autouse=True)
def _reset_dependency_overrides():
    from app.main import app
    saved = dict(app.dependency_overrides)
    yield
    app.dependency_overrides.clear()
    app.dependency_overrides.update(saved)
```

Fixing it centrally rather than in `test_manual_trading_m1e.py` means no other
file can reintroduce the same leak.

## 9. Verification

`test_manual_trading_m1e.py` + `test_position_exit_thresholds.py` together: 21
passed (previously 5 failures). Full suite: the 10 order-dependent failures
disappeared, and — importantly — the 8 tests that `NullPool` had broken stayed
passing, because `NullPool` was not used.

## 10. Lessons Learned

- **A fix that resolves the failures you are looking at can still be wrong.**
  Always re-run the entire suite, not the subset under investigation. `NullPool`
  looked like a clean win against a 10-failure sample.
- **Bisection beats theorising.** Twenty minutes of mechanical file bisection
  identified the exact polluter; the plausible-sounding pooling theory had
  already consumed more time than that and was wrong.
- Global mutable state in a test process — `app.dependency_overrides`,
  `app.state`, module-level caches — needs teardown-based restoration, not
  setup-based clearing. Setup-based clearing always leaks the last writer.
- Tests that pass alone and fail together are not flaky. They are
  order-dependent, which is a different and more tractable problem.

## 11. Open Questions

- Whether other files leak `app.state` attributes beyond the two
  (`session_factory`, `order_manager`) that `_restore_trading_app_state` covers
  is **not established**. `app.state.client` and `app.state.ibkr_adapter` are
  both assigned by tests and are **not** restored.
