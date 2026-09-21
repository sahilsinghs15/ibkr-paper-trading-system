# Chapter 10 — Kill Switch

## Current Implementation

### Responsibility

Emergency flattening, and blocking new exposure on an account until an operator
explicitly clears it. `KillSwitchService` is the sole owner of emergency
flattening (`AGENTS.md` §4).

### Scopes

Kill-switch operations are **scoped**. This is the single most important fact
about the current design and the source of most confusion when reading older
code or tests.

| Scope | Constant | Armed by |
|---|---|---|
| `engine` | `KILL_SWITCH_SCOPE_ENGINE` | Operator square-off of signal/engine positions |
| `manual` | `KILL_SWITCH_SCOPE_MANUAL` | Kill Manual — flatten manual positions |
| `account` | `KILL_SWITCH_SCOPE_ACCOUNT` | Complete Flatten; emergency webhook |

The armed-uniqueness index is `(account_id, scope)`
(`uq_kill_switch_operations_armed_account_scope`, migration `l1m2n3o4p5q6`),
partial over the armed statuses. Two operations on the same account may coexist
if their scopes differ. Idempotency is enforced **within** a scope, not across
scopes.

### What each scope blocks

Scope determines which *ledger* is halted. This is not cosmetic: each scope also
determines which positions the flatten touches, and the two must agree.

| Scope | Engine signals | Manual ticket | Flattens |
|---|---|---|---|
| `engine` | blocked | **open** | engine ledger only |
| `manual` | open | blocked | manual ledger only |
| `account` | blocked | blocked | both, when armed by the operator route (see below) |

`engine` deliberately leaves manual trading open. The engine flatten preserves
manual positions by design — `_execute_flatten_operation()` logs
`"Kill Switch manual positions preserved"` — so blocking the manual ticket
would leave the operator holding manual exposure with no way to close it. The
block must be no wider than the flatten.

Predicates, one per ledger (`kill_switch.py`):

| Predicate | Ledger | True for scopes |
|---|---|---|
| `is_account_kill_switch_active()` | engine | `engine`, `account` |
| `is_manual_trading_blocked()` | manual | `manual`, `account` |
| `is_account_scope_kill_switch_active()` | — | `account` only |

`is_account_kill_switch_active()` is named for the *account row* it gates, not
for `account` **scope**. That ambiguity caused a defect (case study below); the
docstring now says so explicitly.

### Who arms which scope

| Trigger | Scope | Flatten performed |
|---|---|---|
| Operator: Flatten Signal Positions | `engine` | engine ledger |
| Operator: Flatten Manual Positions | `manual` | manual ledger |
| Operator: Complete Flatten | `account` | **whole IBKR account** via `run_flatten_gateway_positions`, then ledger convergence |
| Emergency webhook | `account` | none — arms only |
| `RiskExitMonitor`, daily target/stop breach | `account` | engine ledger only |

Two of these arm `account` scope without a whole-account broker flatten, which
is deliberate and worth understanding before changing either.

`arm_account_kill_switch_only()` does exactly what its name says: it creates the
operation, captures the engine+manual snapshot, and arms both caches. It places
**no orders**. The broker flatten in the operator route is a separate
`run_flatten_gateway_positions()` call in the endpoint, not part of arming.

For the daily-risk breach the separation is the point. The breach is an
account-level event, so it must halt both ledgers — otherwise an account blows
its daily stop, the engine book auto-flattens, and the operator can still open
fresh manual risk on the same account. But it must not *auto-flatten* manual
lots: placing broker orders against operator-owned positions stays an explicit
operator action (`AGENTS.md` §7). So it arms `account` scope for the block and
runs `execute_flatten_operation_background()`, which squares off the engine
ledger only.

That leaves an `account`-scope operation whose flatten covered one ledger.
**Confirmed safe:** `_reconcile_and_finalize()` reads `positions` rows and never
inspects `manual_positions`, and `close_all_ledger_after_account_flatten()` —
which `retry_unresolved_operations()` calls for account scope — requires
broker-flat evidence per symbol before closing any row and skips otherwise.
Neither can falsely close a manual lot that is still open at the broker.

### API surface

| Endpoint | Scope | Status |
|---|---|---|
| `POST /config/accounts/{id}/square-off?scope=engine` | engine | 202 |
| `POST /config/accounts/{id}/square-off-manual` | manual | 202 |
| `POST /config/accounts/{id}/square-off-account` | account | 202 |
| `POST /config/accounts/{id}/kill-switch/clear` | all | 200 |
| `POST /emergency-kill-switch` | account | 200 |

**All flatten endpoints return 202 Accepted.** The broker work is still in
flight when the response is sent. This is load-bearing for the UI — see the
ghost-positions case study below.

### Armed status invariant

**Completing a flatten does not disarm the kill switch** (ADR 6). The account
stays blocked until an operator clears it. This prevents an automated strategy
re-entering immediately after an emergency exit.

Engine and account scopes deliberately stay armed on `UNRESOLVED`. Manual scope
is deliberately the opposite — see the cache case study.

### Flatten snapshot

Arming captures an immutable snapshot of open engine and manual positions
(`kill_switch_flatten_snapshots`, migration `c1d2e3f4a5b6`) so later
reconciliation works against the set that existed *at arming time*, not
whatever is open later.

### Hot caches

Three in-memory sets, all derived and never authoritative (`AGENTS.md` §5):

| Cache | Armed by scope | Hydration predicate |
|---|---|---|
| `_KILL_SWITCH_ACTIVE_ACCOUNTS` | `engine`, `account` | scope ∈ (engine, account) ∧ `_ARMED_STATUSES` |
| `_ACCOUNT_SCOPE_KILL_SWITCH_ACCOUNTS` | `account` | scope = account ∧ `_ARMED_STATUSES` |
| `_MANUAL_KILL_SWITCH_ACTIVE_ACCOUNTS` | `manual` | scope = manual ∧ `_MANUAL_FLATTEN_ACTIVE_STATUSES` |

Each must be reconstructable by `hydrate_kill_switch_cache()` from its
predicate. Both defects recorded below were a cache and its predicate
disagreeing — once in time (release condition), once in scope.

`clear_account_kill_switch_cache()` clears all three; callers must use it rather
than discarding from one set directly, which is how account deletion previously
left stale ids behind.

### Failure behavior

Convergence is eventual: `UNRESOLVED` operations are retried
(`order_manager.py`, and `test_kill_switch_eventual_convergence.py`).
Account-flatten ledger closure requires broker-flat verification **and**
execution evidence (`ffb16bd`), not merely an assumption that orders were sent.

### What counts as evidence a lot was flattened

Reconciliation resolves a manual lot from one of two things, in order:

1. **Exact linkage** — a `ks_manual` close order that reached `FILLED`, its
   `manual_executions` rows summing to the snapshotted quantity. Unambiguous;
   used whenever available.
2. **Per-symbol broker comparison** — the fallback when the close order never
   reached a terminal status: `broker_qty[sym] == engine_qty[sym]` implies the
   manual lot is gone.

Route 2 mixes two different clocks. `broker_qty` comes from the
`broker_positions` snapshot, stamped `as_of`; `engine_qty` is live `positions`
state. Because manual scope does not block engine signals, on a shared symbol
those can describe different moments. It is therefore only trusted when:

- the snapshot post-dates the close order's `submitted_at` (a snapshot taken
  before the order went out cannot evidence that it filled); and
- no engine position on that symbol opened or closed at or after `as_of`.

Otherwise the lot is left unresolved for the next retry. The branch **mutates
status**, so a comparison that cannot be stood behind must not be acted on:
marking a still-open lot `FLATTENED_PENDING_PRICE` tells the operator it is
flat. See the reconciliation-race case study.

### Current limitations

- Notification emission still uses a bare `loop.create_task(...)` whose result
  is not retained (`kill_switch.py` ~line 298). The *flatten* task is correctly
  retained in `self._in_flight` with a done-callback; the notification one is
  not. Lower severity, but the same shape as finding C7.

---

## Engineering History

### Chronology

| Date | Change | Commit / migration |
|---|---|---|
| 2026-08-19 | Kill switch introduced | `d62a8cd` |
| 2026-08-21 | Durable emergency kill-switch operations; repair script for historical positions | `f49a093`, `323d138` |
| 2026-08-26 | Authenticated emergency webhook | `c6aad09` |
| 2026-09-03 | Review findings C1, C2, C3, C7 target the kill switch | `448a9e2` |
| — | Partial unique index: one armed operation per account | migration `i3j4k5l6m7n8` |
| 2026-09-15 | Dual-option kill switch: engine and account-wide | `0272637` |
| 2026-09-16 | Eventual convergence retries, row-level locking, engine/manual isolation | `29fce78` |
| 2026-09-16 | Account-flatten ledger close requires broker-flat verification | `ffb16bd`, `0e9dbc7`, `cf79bfc` |
| 2026-09-17 | Manual position scope added to models, services, migrations, UI | `05cf74a` |
| — | Scope column; armed index becomes `(account_id, scope)` | migration `l1m2n3o4p5q6` |
| 2026-09-21 | Compensation enabled during active kill switch; manual cache release corrected | `64f3009` |
| 2026-09-21 | Scope isolation: engine scope no longer blocks manual trading; account scope gains its own cache; daily-risk breach escalated to account scope | uncommitted at time of writing |
| 2026-09-21 | Manual reconciliation stops trusting a cross-instant broker/engine comparison | uncommitted at time of writing |

### From check-then-act to a database constraint

Finding C1 (P0, "certain", 2026-09-03) recorded:

> No unique constraint on `kill_switch_operations`; the "strict idempotency"
> check is a plain check-then-act.
>
> Two `KILL_SWITCH_ACTIVATED` events, two flatten workers, every open pair
> reversed twice — account flips from flat to an equal and opposite position.

The remedy is visible in the migration sequence: first a partial unique index
on armed operations per account (`i3j4k5l6m7n8`), then — when scopes were
introduced — the index widened to `(account_id, scope)` (`l1m2n3o4p5q6`).

**Confirmed:** the index exists today. This is the clearest instance in the
repository of the system's central lesson: an application-level "check for an
existing row, then insert" is not idempotency. Only the database can serialise
that, and the fix is a constraint, not more careful code.

---

# Case Study: Manual kill-switch cache released only on COMPLETE

**Date:** 2026-09-21
**Feature:** Kill Switch — manual scope
**Type:** Data Integrity Fix
**Severity:** P1
**Status:** Fixed
**Related Components:** `KillSwitchService`, `_MANUAL_KILL_SWITCH_ACTIVE_ACCOUNTS`, `hydrate_kill_switch_cache`

---

*Established by:* commit `64f3009` (2026-09-21) and the module docstring of
`backend/tests/test_kill_switch_cache_and_unwind.py`, which states the defect
and its consequences directly. This case study was **not** authored by the
engineer who diagnosed it; sections 3 and 5 are therefore thinner than the
template wants, and that is marked rather than filled in.

## 1. What Was Happening?

An `UNRESOLVED` manual flatten blocked manual order submission **in-process
forever**, while a restart silently unblocked it.

## 2. Expected Behavior

The in-memory cache must mirror exactly the database predicate used to rebuild
it. A restart must not change the system's answer to "is manual trading blocked
on this account?"

## 3. Initial Understanding

**Not established by available material.**

## 4. Symptoms / Evidence

From the test module docstring:

> `_reconcile_and_finalize_manual` discarded the account from
> `_MANUAL_KILL_SWITCH_ACTIVE_ACCOUNTS` only when `unresolved == 0`. An
> UNRESOLVED manual flatten therefore blocked manual order submission
> in-process forever, while `hydrate_kill_switch_cache` (which rebuilds from
> `_MANUAL_FLATTEN_ACTIVE_STATUSES`, excluding UNRESOLVED) silently unblocked it
> after a restart. That also locked the operator out of the manual closes needed
> to resolve the operation.

The last sentence is the operationally serious part: the state that needed
manual intervention was the state that prevented manual intervention.

## 5. Investigation

**Not recorded in available material.** The defect is stated as a conclusion.

## 6. Root Cause

**Confirmed root cause:** the release condition and the hydration predicate
disagreed. Release happened only on `COMPLETE`; hydration rebuilt from
`_MANUAL_FLATTEN_ACTIVE_STATUSES`, which excludes `UNRESOLVED`. The in-memory
answer and the post-restart answer therefore differed for exactly the
`UNRESOLVED` case.

**Design note, confirmed:** engine scope is deliberately the opposite —
`UNRESOLVED` stays armed until an explicit operator clear (ADR 6). The two
scopes genuinely need different behaviour, which is why this was not caught by
symmetry.

## 7. Code-Level Location

**File:** `backend/app/services/kill_switch.py`
**Class:** `KillSwitchService`
**Functions:** `_reconcile_and_finalize_manual()`, `_update_operation_completion()`

**Call path:**

```text
KillSwitchService._reconcile_and_finalize_manual()
  → unresolved > 0
    → final_status = UNRESOLVED
      → _MANUAL_KILL_SWITCH_ACTIVE_ACCOUNTS.discard() skipped   [defect]
        → manual orders blocked in-process
          ↕ disagrees with
        hydrate_kill_switch_cache()  → rebuilds without UNRESOLVED
```

## 8. The Fix

Release moved into `_update_operation_completion()`, made the single owner of
the transition, so the cache mirrors `_MANUAL_FLATTEN_ACTIVE_STATUSES` by
construction. The three scattered `discard()` calls were removed. The comment
on the cache declaration now names the owner explicitly.

## 9. Verification

`backend/tests/test_kill_switch_cache_and_unwind.py` (379 lines, added in the
same commit).

## 10. Lessons Learned

- **A cache and its rehydration must share one predicate.** If the release
  condition is written separately from the hydration query, they will drift.
  The fix was not to correct the condition in three places but to make one
  function own the transition.
- "Works until restart, then behaves differently" is the signature of a
  cache/source-of-truth disagreement. It is worth testing restart behaviour
  explicitly for any in-memory gate.

## 11. Open Questions

- Whether this was hit in production is **Unknown**.
- The diagnosis path is **not recorded**.

---

# Case Study: Compensation suppressed while the kill switch was armed

**Date:** 2026-09-21
**Feature:** Kill Switch × OMS compensation
**Type:** Bug
**Severity:** P1
**Status:** Fixed
**Related Components:** `BasketCoordinator._compensate_filled`, `KillSwitchService`

---

*Established by:* commit `64f3009` and the docstring of
`backend/tests/test_kill_switch_cache_and_unwind.py`. Sections 3 and 5 are not
recorded.

## 1. What Was Happening?

`_compensate_filled()` returned `[]` for OPEN intents whenever the account kill
switch was active. Compensation is what unwinds a partially-filled basket, so
suppressing it stranded the filled leg naked.

## 2. Expected Behavior

Compensation reduces exposure and must run even — especially — when the kill
switch is armed.

## 3. Initial Understanding

**Not established.**

## 4. Symptoms / Evidence

From the test docstring:

> Compensation emits reverse CLOSE legs that only reduce exposure, so skipping
> it stranded the filled leg naked — an unsettled basket writes no `positions`
> row, so the kill-switch flatten snapshot cannot see it — and forced the basket
> to CRITICAL instead of unwinding to COMPENSATED.

The parenthetical is the sharp edge: the flatten snapshot enumerates `positions`
rows, and an unsettled basket has not written one. So the naked leg was
invisible to the very mechanism that was supposed to be protecting the account.

## 5. Investigation

**Not recorded.**

## 6. Root Cause

**Confirmed root cause:** the kill-switch guard did not distinguish orders that
*add* exposure from orders that *reduce* it. Blocking all OPEN-intent
submissions caught compensation legs, which are reverse CLOSEs.

**Confirmed constraint retained:** the remainder-**retry** guard must stay,
because a retry adds exposure. Only compensation was unblocked.

## 7. Code-Level Location

**File:** `backend/app/oms/coordinator.py`
**Class:** `BasketCoordinator`
**Function:** `_compensate_filled()`

**Call path:**

```text
BasketCoordinator.execute()
  → basket incomplete
    → _compensate_filled(original intent = OPEN)
      → kill switch active for account?
        → return []          [defect: no unwind]
          → basket → CRITICAL, filled leg naked
            → not in positions → invisible to flatten snapshot
```

## 8. The Fix

Compensation is permitted during an active kill switch; the retry guard is
unchanged.

## 9. Verification

`test_kill_switch_cache_and_unwind.py`.

## 10. Lessons Learned

- **Direction matters more than intent type.** "Is this an OPEN?" is the wrong
  question for a risk gate. The right one is "does this increase or decrease
  exposure?" A compensation leg is structurally an OPEN-intent unwind.
- A safety mechanism that creates the state it is meant to prevent is the worst
  category of defect. Here, arming the kill switch could strand a naked leg that
  the kill switch could then not see.

## 11. Open Questions

- Whether this occurred in production is **Unknown**.

---

# Case Study: Positions lingered after a flatten until manual refresh

**Date:** 2026-09-21
**Feature:** Kill Switch — operator UI
**Type:** Operational Fix
**Severity:** P2
**Status:** Fixed
**Related Components:** `KillSwitchModal`, `AccountSettingsPage`, `usePnlStream`, demo stream publisher

---

## 1. What Was Happening?

Operator report:

> I executed all the positions via kill switch. It momentarily showed ghost
> signals, and then I had to refresh the page, and that's when it showed no
> positions, no open positions. Ideally, it should have been done automatically.

## 2. Expected Behavior

After a flatten completes, the dashboard converges on its own.

## 3. Initial Understanding

The first hypothesis was that the SSE publisher was failing to emit
`POSITION_CLOSED`, i.e. a backend defect. Investigation showed the publisher
does emit it correctly — the defect was that the frontend had no resync trigger
and was waiting on a stream event that legitimately had not happened yet.

## 4. Symptoms / Evidence

- Flatten endpoints return **202 Accepted** — broker work still in flight.
- The success handler in `AccountSettingsPage.tsx` invalidated only two config
  queries:

```tsx
void queryClient.invalidateQueries({ queryKey: ['config', 'kill-switch', account.id] })
void queryClient.invalidateQueries({ queryKey: ['config', 'account', cleanAccount] })
```

Neither touches positions, which come from `/demo/positions` and the SSE stream.

- `demo_streaming/publisher.py` **does** emit `POSITION_CLOSED`, for positions
  that vanish from the OPEN set, on its next poll.
- `loadSnapshot()` in `usePnlStream.ts` was module-private with no exported way
  to trigger it.

## 5. Investigation

### Step 1
Traced the modal's `onSuccess` to `AccountSettingsPage`. Found only config
invalidation, nothing for positions.

### Step 2
Checked whether the endpoint is synchronous. All three square-off routes are
declared `status_code=status.HTTP_202_ACCEPTED`. So a single refetch on response
would still show open positions — the flatten has not happened yet.

### Step 3
Checked whether SSE would clean up unaided. The publisher does emit
`POSITION_CLOSED` for vanished OPEN rows. So the system *would* converge — on
the publisher's next poll after the broker sweep lands. The gap between the 202
and that poll is the ghost window, and nothing shortened it.

### Step 4
Concluded the frontend needed an explicit resync, and that a single one would
not be enough given the asynchronous flatten.

## 6. Root Cause

**Confirmed root cause:** the UI had no positions-resync trigger after an
accepted flatten, and the flatten is asynchronous, so the operator saw stale
rows until the SSE publisher's next poll — or until they reloaded the page.

**Not a backend defect.** The publisher behaves correctly.

## 7. Code-Level Location

**Files:** `frontend/src/pages/AccountSettingsPage.tsx`,
`frontend/src/hooks/usePnlStream.ts`

**Call path:**

```text
KillSwitchModal → engineMutation.mutate()
  → POST /square-off?scope=engine → 202 Accepted (flatten still in flight)
    → onSuccess → invalidateQueries(['config', ...]) only   [defect]
      → positions store untouched
        → stale rows until publisher's next poll / page reload
```

## 8. The Fix

Exported `resyncPositions()` from `usePnlStream.ts` and added
`scheduleFlattenResync()`, which re-pulls the snapshot on a short backoff
(`0, 1.5s, 4s, 8s`) covering a typical flatten plus publisher poll. Called from
the kill-switch `onSuccess`.

Presentation-layer only: it re-reads authoritative state and synthesizes
nothing, consistent with `AGENTS.md` §11.

## 9. Verification

`frontend/e2e/killSwitchResync.spec.ts`, with routes mocked so one open
position is served until the flatten is accepted and none afterwards.

**The first version of this test passed without the fix** — a false positive.
The mocked SSE stream was fulfilled with an empty body, which closed it
immediately, causing `EventSource.onerror` and the hook's reconnect path, which
reloads the snapshot on its own. The test was measuring the reconnect, not the
fix. Corrected by leaving the SSE request hanging so only the fix can trigger a
resync, then verified in both directions: **fails** with the fix disabled,
**passes** with it enabled.

## 10. Lessons Learned

- **202 means the work has not happened.** Any UI action whose endpoint returns
  202 needs a convergence strategy, not a single refetch.
- **A regression test that has not been seen to fail is not a regression test.**
  Disabling the fix and re-running is cheap, and here it caught a mock that
  quietly exercised a different code path.
- Mocking a streaming transport requires care: an immediately-closed stream is
  not a quiet stream, it is an error, and error paths often have their own
  recovery logic that masks what you are testing.

## 11. Open Questions

- Whether Close Pair and other 202 endpoints have the same gap is **not
  established**. `scheduleFlattenResync()` is currently wired only to the kill
  switch. Carried into
  [Chapter 21](../part4_ownership/21_current_known_limitations.md).


---

# Case Study: Engine-scope kill switch blocked manual trading

**Date:** 2026-09-21
**Feature:** Kill Switch — scope isolation
**Type:** Bug
**Severity:** P1
**Status:** Fixed
**Related Components:** `KillSwitchService`, `ManualTradingService.validate_pretrade`, `RiskExitMonitor`

---

*Established by:* the code as it stood before the change, and
`backend/tests/test_kill_switch_scope_isolation.py`. The diagnosis in sections
3–5 was carried out in-session and is recorded here in the order it actually
happened, including the part that was overstated.

## 1. What Was Happening?

Arming the **engine** kill switch ("Flatten Signal Positions") blocked the
operator out of the **manual** ticket entirely. The engine flatten does not
close manual positions — it explicitly preserves them — so the operator was
left holding live manual exposure that the dashboard would not let them close.

## 2. Expected Behavior

Each scope blocks only the ledger it flattens:

- `engine` → engine signals only
- `manual` → manual orders only
- `account` → both

## 3. Initial Understanding

The question that started this was an operator question, not a bug report:
*"what happens if I try to do manual trading from the dashboard when the kill
switch is on?"* The first pass through `validate_pretrade()` read the gate as
three distinct branches and reported them as three distinct behaviours.

That was wrong, and re-reading the arming code is what corrected it: `engine`
and `account` scope both call `_arm_kill_switch_cache()`, so both land in the
**same** set. From the manual ticket's point of view they were indistinguishable
— identical branch, identical message. The "three behaviours" were two.

A second claim made in the same pass was also too strong. The stranded-leg
analysis asserted the naked leg fell to `PositionReconciler` /
`broker_align_service`. It does not: `_fail_critical()` calls
`schedule_recovery()` on `CriticalRecoveryService`, which is a dedicated
flatten-and-unlock path. The leg was recovered; the severity was lower than
first stated. Recorded here because the wrong version is the more instructive
one — a component that exists is easy to miss when reasoning from a call path
you have only read downward.

## 4. Symptoms / Evidence

`manual_trading.py`, before the change:

```python
if is_account_kill_switch_active(account.id):
    errors.append(f"Account {...} kill switch is active. Trading is blocked.")
elif is_manual_kill_switch_active(account.id):
    ...
```

`is_account_kill_switch_active()` is true for `engine` **and** `account` scope,
because `initiate_square_off()` (engine) and `arm_account_kill_switch_only()`
(account) both call `_arm_kill_switch_cache()`. There was no predicate that
meant "account scope only".

Compounding it, the gate has no reducing-order exemption — unlike trading
pause, which permits an order that genuinely reduces an open manual lot. So the
block covered even the close the operator needed.

## 5. Investigation

### Step 1
Traced every consumer of `is_account_kill_switch_active()`. Six are engine-path
gates (`order_manager`, `coordinator`, `risk_exit_monitor`, `red_zone_release`),
two are the manual gate, the rest are audit/response fields. Only the manual
two were wrong — which is why the predicate could be left alone and a second
one added, rather than renamed across the tree.

### Step 2
Checked whether the escape hatch was real. `initiate_manual_square_off()` has no
engine-scope precondition, so **Flatten Manual Positions** still worked while
the engine switch was armed, as did cancelling a resting manual order
(`cancel_order()` does not call `validate_pretrade()`). The operator was not
fully trapped, but the obvious route — closing the lot from the ticket — was
shut.

### Step 3
Looked for a test pinning the old behaviour, since changing it would break one.
`test_manual_trading_m1b.py::test_manual_halt_and_kill_switch_block_order`
inserted a `KillSwitchOperationModel` with no `scope`, which defaults to
`engine` (`server_default="engine"`), and asserted the manual preview was
blocked. It encoded exactly the behaviour being removed.

### Step 4
Checked what the automatic daily-risk path arms.
`RiskExitMonitor._handle_account_breach()` called `initiate_square_off()` —
**engine** scope. Under the new rule that would let an account breach its daily
stop, auto-flatten the engine book, and still accept new manual orders. This was
flagged for decision rather than changed silently, and subsequently escalated to
account scope.

### Step 5
Before escalating it, checked what `arm_account_kill_switch_only()` actually
does. Despite the name, it **arms and snapshots but places no orders** — the
broker flatten in the account-scope route is a separate
`run_flatten_gateway_positions` call. So swapping the call naively would have
armed the switch and flattened *nothing*. The retained
`execute_flatten_operation_background()` call is what still squares off the
engine book.

### Step 6
Verified that an `account`-scope operation whose flatten only covered the engine
ledger is safe. `_reconcile_and_finalize()` reads `positions` rows and never
inspects `manual_positions`; `close_all_ledger_after_account_flatten()` — which
`retry_unresolved_operations()` calls for account scope — requires broker-flat
evidence per symbol before closing any row, and skips otherwise. Neither can
falsely close a manual lot that is still open at the broker.

## 6. Root Cause

**Confirmed root cause:** two scopes shared one cache. `engine` and `account`
both armed `_KILL_SWITCH_ACTIVE_ACCOUNTS`, and the manual gate had no finer
predicate to ask, so it treated the union as "kill switch on". The naming
reinforced it — `is_account_kill_switch_active()` reads like "account scope" and
means "this account's engine ledger is halted".

## 7. Code-Level Location

**Files:** `backend/app/services/kill_switch.py`,
`backend/app/services/manual_trading.py`

**Call path:**

```text
POST /config/accounts/{id}/square-off?scope=engine
  → initiate_square_off()            scope = ENGINE
    → _arm_kill_switch_cache()       → _KILL_SWITCH_ACTIVE_ACCOUNTS
                                        ↑ same set as account scope
POST /manual/orders/preview
  → validate_pretrade()
    → is_account_kill_switch_active()  → True   [defect]
      → "kill switch is active. Trading is blocked."
        → operator cannot close the manual lot the engine flatten left open
```

## 8. The Fix

A third cache, `_ACCOUNT_SCOPE_KILL_SWITCH_ACCOUNTS`, armed only by
`arm_account_kill_switch_only()` — on both its creation path *and* its
idempotent early-return path, since the latter otherwise left a cold cache
unblocked. Two ledger-specific predicates, `is_manual_trading_blocked()` and
`is_account_scope_kill_switch_active()`. The manual gate switched to the former,
and its database fallback narrowed to `scope=account`.

Supporting corrections found while making it: account deletion discarded from
one cache directly rather than calling `clear_account_kill_switch_cache()`, and
the test-isolation fixture cleared only one of the sets.

`RiskExitMonitor._handle_account_breach()` now arms **account** scope — a daily
target/stop breach is an account-level event and must halt both ledgers — while
still flattening only the engine ledger. Manual lots are blocked from further
trading but never auto-flattened: placing broker orders against operator-owned
lots stays an explicit operator action (`AGENTS.md` §7).

## 9. Verification

`backend/tests/test_kill_switch_scope_isolation.py` — ten cases: each scope
blocks only its own ledger at both the predicate level and through
`validate_pretrade()`; isolation survives `hydrate_kill_switch_cache()`; the
idempotent account re-arm keeps manual blocked; per-scope clearing releases only
that scope; a daily-risk breach blocks manual **and** leaves the manual lot open
with zero `manual_orders` rows written.

`test_manual_halt_and_kill_switch_block_order` was rewritten rather than
deleted: it now asserts engine scope leaves the preview **valid**, then
escalates to account scope and asserts it is blocked. Both rows are written
straight to PostgreSQL with no cache arming, so it still covers the
database-fallback branch of the gate.

## 10. Lessons Learned

- **One cache per question, not one cache per subsystem.** The moment two
  scopes shared a set, the system lost the ability to answer "which ledger is
  halted?" — and the manual gate had no choice but to over-block.
- **A block must be no wider than the action that justifies it.** The engine
  flatten deliberately spares manual positions; the engine block did not. A
  halt that stops the remedy is worse than no halt.
- **Read the arming code, not just the gate.** The gate looked scope-aware —
  three branches, three messages. The conflation was two function calls away,
  in code that decides what "active" means.
- **A name that reads like a scope should be a scope.**
  `is_account_kill_switch_active()` survived because renaming it would have
  touched every engine path; the mitigation was an explicit docstring and a
  differently-named sibling. Acceptable, but it is why the defect was invisible
  on review.

## 11. Open Questions

- Whether an operator hit this in production is **Unknown**.
- `AccountConfigSchema.kill_switch_active` still reports the union of engine and
  account scope, because it backs an account-level status badge rather than a
  ledger gate. Whether the dashboard should show the two scopes separately is
  **not established** and is left open.

---

# Case Study: Manual reconciliation trusted a cross-instant broker comparison

**Date:** 2026-09-21
**Feature:** Kill Switch — manual scope reconciliation
**Type:** Data Integrity Fix
**Severity:** P1
**Status:** Fixed
**Related Components:** `KillSwitchService._reconcile_and_finalize_manual`, `broker_positions`, `PositionModel`

---

*Established by:* the code as it stood before the change and
`backend/tests/test_kill_switch_manual_reconcile_race.py`. Found by reasoning
about scope isolation rather than from an incident, so section 4 is analysis,
not a production trace — marked accordingly.

## 1. What Was Happening?

When a manual flatten's close order never reached `FILLED`, reconciliation fell
back to inferring the outcome from a per-symbol quantity comparison:

```python
broker_actual = broker_qty_by_symbol.get(norm_sym, Decimal(0))
engine_sym_qty = engine_qty_by_symbol.get(norm_sym, Decimal(0))
if broker_actual == engine_sym_qty and broker_rows:
```

`broker_qty_by_symbol` is built from `broker_positions`, a snapshot of IBKR
stamped `as_of`. `engine_qty_by_symbol` is read live from `positions` in the
current transaction. The equality was treated as proof the manual lot was gone.

## 2. Expected Behavior

Two quantities may only be compared if they describe the same moment, and the
snapshot must be new enough to have observed the close order it is being used to
evidence. Where neither holds, the lot stays unresolved and is retried.

## 3. Initial Understanding

This surfaced while answering an operator question about manual/signal kill
switch interaction, not from an alert. The first framing was "manual scope does
not block engine signals — is that safe?" The block itself is safe; the
reconciliation built on top of it is where the shared symbol bites.

Worth recording: the same function's *docstring* already advertised the
shared-symbol handling as a feature —

> Shared-symbol broker check: compares broker signed quantity against
> (engine_signed_qty + remaining_manual_signed_qty). Engine positions are
> preserved!

— which is why it reads as deliberate. It is deliberate about *which* ledgers to
compare. It is silent about *when* each was observed, and that is the defect.

## 4. Symptoms / Evidence

**Analysis, not an observed incident.** The failing sequence:

1. Manual lot: +50 AAPL. Manual flatten arms, close order submitted, not yet
   terminal.
2. Broker snapshot lands: AAPL = 0 (or any value that happens to match).
3. An engine signal — unblocked, correctly, by scope isolation — opens or closes
   an AAPL leg.
4. Reconciliation compares snapshot AAPL against *live* engine AAPL. They agree
   by coincidence of two different instants.
5. No `manual_executions` rows yet, so the lot is written
   `FLATTENED_PENDING_PRICE`.

Step 5 is the damage. The lot is still open at IBKR, and the ledger now says the
broker confirmed it closed and only the price is outstanding. A second variant
needs no engine activity at all: a snapshot predating the manual lot's own
opening also compares equal.

`FLATTENED_PENDING_PRICE` counts toward `unresolved`, so the operation is
retried — but the status mutation has already been written and is what the
operator sees.

## 5. Investigation

### Step 1
Checked whether a single transaction fixes it. It does not. Both reads already
happen inside one `session.begin()`. The inconsistency is not transactional:
`broker_positions` is a *materialised snapshot* of an external system at T1,
while `positions` is local state at T2. No isolation level reconciles those.

### Step 2
Looked for a timestamp to anchor on. `BrokerPositionModel.as_of` exists, and
`BrokerPositionRepository.replace_snapshot()` stamps every row of one sweep with
the same `snapshot_time`, so `max(as_of)` is a usable observation instant.

### Step 3
Looked for the engine-side equivalent. `positions` has no generic `updated_at`,
only `opened_at` and `closed_at` — sufficient here, because an engine pair lot is
opened once with fixed leg quantities and closed once. A symbol is "moved since
the snapshot" if any row on it has `opened_at >= as_of` or `closed_at >= as_of`.
Note the closed case must be included: a pair closed after the snapshot is absent
from the live OPEN set while still counted in the snapshot.

### Step 4
Established the second, engine-independent failure: a snapshot older than the
close order. Fixed by requiring `as_of >= max(close_order.submitted_at)`, falling
back to the operation's `created_at` when no close order exists.

### Step 5
Checked the sibling path. `close_all_ledger_after_account_flatten()` compares
broker quantity against **zero**, not against engine quantity, and additionally
requires FIFO execution evidence (`ffb16bd`). It does not have this defect.

### Step 6
Checked that holding back cannot stall convergence. Broker snapshots refresh on
the reconcile sweep, so `as_of` advances and the guard clears on a later pass;
`retry_unresolved_operations()` re-runs manual reconciliation every 30s. The
exact-linkage route is unaffected and remains primary. Pinned by a test that
resolves the lot on the second pass.

## 6. Root Cause

**Confirmed root cause:** an equality between a snapshot of an external system
and live local state, with nothing establishing that the two referred to the same
moment. Scope isolation — correct in itself — is what makes the two clocks drift,
because engine signals keep mutating `positions` throughout a manual flatten.

## 7. Code-Level Location

**File:** `backend/app/services/kill_switch.py`
**Function:** `_reconcile_and_finalize_manual()`

```text
close order not FILLED
  → fallback: broker_qty[sym] == engine_qty[sym]?
      broker_qty  ← broker_positions snapshot @ as_of        (T1)
      engine_qty  ← positions, live                          (T2)
        → equal by coincidence across T1/T2
          → no executions yet
            → status = FLATTENED_PENDING_PRICE   [defect]
              → operator told a live position is flat
```

## 8. The Fix

The fallback is gated on evidence quality rather than removed. It now requires
the snapshot to post-date the close order **and** the symbol to be untouched by
the engine since `as_of`; otherwise the lot is left unresolved for the retry,
with the reason logged. The exact-linkage route is unchanged and still primary.

## 9. Verification

`backend/tests/test_kill_switch_manual_reconcile_race.py` — five cases, split
deliberately: two pin that the gate does **not** block legitimate resolution
(fresh snapshot + quiet engine still closes with correct PnL; trusted evidence
with no fills still yields `FLATTENED_PENDING_PRICE`), two pin each race variant,
and one pins that a held-back lot resolves on the next sweep.

Verified in both directions: with the gate reverted, the three race cases fail
(`OPEN` vs `FLATTENED_PENDING_PRICE`) and the two no-regression cases still pass.

## 10. Lessons Learned

- **Two numbers from two clocks are not comparable, whatever the isolation
  level.** A database transaction makes local reads consistent with each other;
  it says nothing about when an external system was observed. Anything derived
  from a broker snapshot needs its `as_of` carried into the decision.
- **Ask what a branch writes before asking whether it is right.** This one
  mutated position status, which is why a coincidental equality became a lie
  told to the operator rather than a harmless retry.
- **Isolating two subsystems creates concurrency between them.** Scope isolation
  was the correct fix for the previous defect; it also guaranteed that engine
  writes now land during manual reconciliation. A fix that removes a lock should
  prompt the question of what else assumed that lock.
- Where evidence is ambiguous and a retry loop already exists, declining to
  decide is nearly free. The cost of waiting one sweep is far below the cost of
  a wrong status.

## 11. Open Questions

- Whether this was ever hit in production is **Unknown**; it was found by
  analysis.
- The guard uses `opened_at`/`closed_at` because engine pair lots do not mutate
  leg quantities in place. If partial closes that mutate `leg_*_signed_qty` are
  ever introduced, this guard must gain an `updated_at` to remain correct.
  Carried to [Chapter 21](../part4_ownership/21_current_known_limitations.md).

