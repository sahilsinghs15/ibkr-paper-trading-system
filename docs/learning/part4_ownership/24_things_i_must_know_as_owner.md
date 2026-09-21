# Chapter 24 — Things I Must Know as Owner

The short list. If you read nothing else in this documentation set, read this.

---

## Before you change anything

1. **Read `AGENTS.md` first.** It is the governance contract, not an
   introduction. Sections 6 (do-not-bypass), 8 (database safety) and 9 (identity
   model) are the ones most often violated by well-intentioned changes.

2. **Search [Chapter 22](22_important_invariants.md) before deleting anything
   that looks defensive.** Most of it is load-bearing, and the table tells you
   which incident put it there.

3. **Identify the single owner.** Every mutation has exactly one authoritative
   component (`AGENTS.md` §4, and [Chapter 20](20_current_architecture.md)).
   If your change writes a table from a second place, it is wrong.

---

## The five things most likely to hurt you

### 1. There is one broker socket and one rate limiter

All accounts share them. "Isolation" between accounts is logical, not physical.
Never create a second `TWSClient`. Never call `placeOrder` without a rate
limiter token.

### 2. Retries are not legs

A retry creates a new `orders` row with the **same** `leg_index` and the
**remaining** quantity. Group by `basket_id` + `leg`, take `max` for required
and `sum` for filled. Counting rows gives the wrong answer, and has.

### 3. Kill-switch operations are scoped

Engine, manual and account are three separate scopes with independent armed
state. Idempotency is **within** a scope. Code or tests that assume one kill
switch per account are pre-2026-09-17 and are wrong.

Engine and account scopes stay armed on `UNRESOLVED` by design. Manual scope
does not. That asymmetry is deliberate — see Chapter 10.

### 4. Flatten endpoints return 202

The broker work is still in flight when the response arrives. Anything that
reacts to a flatten needs to converge, not assume.

### 5. In-memory caches must match their rehydration query

`_KILL_SWITCH_ACTIVE_ACCOUNTS`, `_MANUAL_KILL_SWITCH_ACTIVE_ACCOUNTS`,
notification flapping windows. If a cache's release condition and its hydration
predicate disagree, the system answers differently before and after a restart.

---

## Local development

- Backend: `backend/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8001`
  (or `uv run uvicorn ...`, which execs the same binary).
- Database: Postgres in container `ibkr-postgres` on **port 5433**, database
  `ibkr_trading`. Tests use a **separate** database, `ibkr_trading_test`
  (`backend/tests/conftest.py`), so pytest never touches dev data.
- **There are no systemd units locally.** They exist on the deployment host.
  `systemctl` calls fail with "Access denied" on a dev machine — this is
  expected, not a bug. Service-control behaviour is covered by mocked tests; do
  not invoke the real service manager to "check" it.
- The IB Gateway may be unavailable. The backend starts and degrades gracefully;
  audit, settings, search and most tests work without it.

---

## Running tests

```bash
cd backend && uv run pytest -q          # ~4.5 min, 1430 tests
```

- Tests that pass alone and fail together are **order-dependent**, not flaky.
  Bisect with `pytest --collect-only` to get the order, then halve.
- Use `--tb=line` when investigating. `--tb=no` discards assertion messages,
  and several tests here carry diagnostics in them.
- Execution-path tests are neutralised against the Red Zone by an autouse
  fixture. Opt out with `@pytest.mark.real_session_clock` if you need the real
  clock.

---

## When something looks broken

1. **Establish what the system actually did** before assuming a regression.
   Check the logs for a line explaining the behaviour — the red-zone deferrals
   announced themselves clearly and were still initially read as failures.
2. **Check whether the test is wrong.** On the most recent full investigation,
   47 of 47 failures were test defects.
3. **Bisect rather than theorise** for order-dependent problems.
4. **Confirm a fix by disabling it** and watching the test fail.

---

## What is not currently known

[Chapter 21](21_current_known_limitations.md) is not a formality. As of
2026-09-21, **8 of 10 P0 findings from the 2026-09-03 review have not been
re-verified.** Nobody can currently say whether they are live.

If you own this system, that list is the first thing to close.

---

## Do not

- Wipe or truncate `orders`, `executions`, `positions`, `trade_executions`,
  `event_log`, `manual_audit_events`, `audit_events` or `auth_sessions`
  (`AGENTS.md` §8). `audit_events` will refuse at the database level anyway.
- Run live order tests against a real or paper gateway without explicit
  instruction (`AGENTS.md` §7).
- Set `TRADINGAPP_TESTING=1` on any production or live-order process.
- Merge `positions` and `manual_positions` without full migration planning
  (ADR 5).
- Build a multi-gateway pool without an explicit architectural mandate (ADR 2).
