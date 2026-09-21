# Chapter 23 — Operational Lessons

Patterns that recur across the history. Each is drawn from at least two
independent incidents.

---

## 1. Check-then-act is not idempotency

Reading "no active operation exists" and then inserting is not atomic. Two
requests both read, both find nothing, both insert.

**Seen in:** finding C1 (two kill switches arm and both flatten); the manual
order idempotency race.

**What works:** a database constraint. `uq_kill_switch_operations_armed_account_scope`
and `uq_manual_orders_account_idempotency` do what careful application code
cannot. Handle the violation as a normal outcome, not an error.

---

## 2. "Could not determine" is not "determined to be false"

A check with two failure modes — answered no, and could not answer — usually
needs both handled the same way. Treating "could not answer" as success is a
recurring defect shape.

**Seen in:** the heartbeat exception (Ch. 4); the watchdog 401 read as a safety
verdict; broker recovery inferred rather than probed (Ch. 13).

**What works:** name the inconclusive case explicitly, then decide deliberately.
Trading path → fail closed. Alerting path → stay quiet.

---

## 3. A cache and its rehydration must share one predicate

If the release condition is written separately from the hydration query, they
will drift, and the symptom will be "works until restart, then behaves
differently".

**Seen in:** the manual kill-switch cache (Ch. 10).

**What works:** one function owns the transition. Not "fix the condition in
three places".

---

## 4. 202 means it has not happened yet

An accepted request is not a completed one. Any UI action behind an async
endpoint needs a convergence strategy.

**Seen in:** ghost positions after a flatten (Ch. 10).

**What works:** re-read authoritative state on a short backoff. Never
synthesize the expected end state client-side.

---

## 5. Row count is not domain cardinality

A table records events; the domain has entities. `orders` rows are submissions;
legs are legs.

**Seen in:** retries rendered as extra legs (Ch. 7).

**What works:** group by the domain key (`leg_index`), and if one side already
derives it correctly, mirror that derivation rather than reimplementing it.

---

## 6. Requesting and observing are different facts

Do not close a ledger row because you asked for something.

**Seen in:** account-flatten ledger closure requiring broker-flat verification
(`ffb16bd`); broker recovery requiring `broker_connection` ready; the audit
recorder separating intent from outcome.

**What works:** record both, separately, and make the outcome conditional on
evidence.

---

## 7. A failing test is evidence about the test as often as the system

Of 47 test failures investigated on 2026-09-21, **all** were test defects. The
system was correct in every case.

**Seen in:** wall-clock dependence (37); leaked dependency override (10);
pre-scope kill-switch semantics (1); assertions on a removed notification path
(3).

**What works:** establish what the system actually did *before* assuming a
regression. The red-zone failures all carried a log line stating plainly that
the gate had deferred the signal — which was correct behaviour.

---

## 8. A regression test that has never failed proves nothing

**Seen in:** the kill-switch resync test, which passed without the fix because a
mocked SSE stream closed immediately and triggered the reconnect path.

**What works:** disable the fix, confirm the test fails, restore it. It costs a
minute.

---

## 9. Dead code kept "for compatibility" is a trap

Keeping `send_canonical_telegram` importable meant three tests kept patching it,
kept passing the patch, and silently stopped asserting anything.

**Seen in:** Ch. 13.

**What works:** delete it. Breaking loudly at migration time, with context
fresh, is cheaper than a misleading failure weeks later.

---

## 10. A fix that resolves the failures in front of you can still be wrong

`NullPool` fixed all 10 failures under investigation and broke 8 that were not.

**Seen in:** Ch. 16.

**What works:** re-run the whole suite. And prefer bisection over a plausible
theory — the mechanical approach found the real polluter faster than reasoning
about it did.

---

## 11. Sleep is a guess; polling is an assertion

**Seen in:** the flapping test (Ch. 13).

**What works:** wait for the condition, bounded by a deadline. Before
lengthening a wait, confirm the extra time cannot change the verdict — here the
300-second flapping window made it provably safe.

---

## 12. When you add a second way to do something, budget for the first

Manual trading was a necessary capability and generated more follow-up work than
any other change: kill-switch scopes, dual-ledger reconciliation, dual PnL
hydration, dual audit paths.

**Seen in:** Ch. 19.

**What works:** enumerate the components that assumed singularity, before
merging, not after.
