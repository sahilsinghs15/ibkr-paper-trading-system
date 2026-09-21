# Chapter 19 — Architecture Changes

The structural turning points. Each changed what the system *is*, not just how
well it works.

---

## 1. Strategy-based broker → OMS + RMS

**Date:** 2026-08-14 · **Commit:** `902ca4d`

> refactor: replace strategy-based broker system with a centralized Order
> Management System (OMS) and Rule Management System (RMS).

The prototype (Aug 7–13) had strategies talking to a broker abstraction
directly. This commit separated three concerns that had been entangled:
*deciding to trade* (strategy, now external), *deciding it is safe*
(RMS), and *getting it filled* (OMS).

Almost nothing from before this commit survives. When reading old code or old
tests, check whether they predate it.

**Consequence:** every safety gate in the system today hangs off the RMS/OMS
split. There was no single place to put a risk check before this.

---

## 2. Postgres as schema authority

**Date:** 2026-08-17 · **Commits:** `86485a6`, `c186d07`

Alembic introduced. `Base.metadata.create_all()` subsequently forbidden in
application code.

**Consequence:** schema changes became reviewable and ordered — and acquired
their own failure mode, branch drift (see the case study in
[Chapter 2](../part1_understanding/02_database_and_persistence.md)).

---

## 3. Baskets for multi-leg atomicity

**Date:** 2026-08-18 · **Commit:** `6bc05f4`

`BasketModel`, `BasketRepository`, `BasketCoordinator`.

A pair trade is not two orders. Without a basket there is no object that can
answer "is this pair complete?", and therefore nowhere to put retry or
compensation logic. Naked-pair protection (`7815be1`, 2026-08-20) is only
expressible because baskets exist.

**Consequence:** the retry model — retries are new `orders` rows on an existing
leg — dates from here, and is the source of the leg-counting bug in
[Chapter 7](../part1_understanding/07_oms_and_basket_execution.md).

---

## 4. Durable queue + worker pool

**Date:** 2026-08-21 · **Commit:** `4c47e37`

Webhook ingest decoupled from execution via `signal_jobs`, with an async worker
pool claiming jobs.

This is ADR 1 made real. It is also what made the Red Zone gate possible: once
execution is decoupled from receipt, deferring a signal is a normal state rather
than a dropped request.

**Consequence:** claiming, leases, fencing and the OPEN-before-CLOSE ordering
problem (finding C4) all originate here.

---

## 5. Durable execution claims

**Date:** ADR 3; barrier present by the 2026-09-03 review

Engine orders must commit an `execution_claims` row before submitting to IBKR.
In-process dedup sets do not survive a crash.

**Consequence:** the review confirmed this is the mechanism that actually
prevents double submission on the webhook path — and finding C2 is precisely
that three other paths bypass it.

---

## 6. Independent watchdog, then systemd

**Dates:** 2026-08-31 (`8cbe353`), 2026-09-01 (`394e616`, `82252f1`)

A separate watchdog process for health and recovery; then service management
moved from a bespoke `process_manager` to systemd units, with the former
deprecated.

**Consequence:** service lifecycle became observable and controllable from
outside the application, which is what made operator service controls — and
their audit requirement — possible.

---

## 7. Manual trading as a first-class path

**Date:** 2026-09-11 · **Commit:** `837baf2`

A second, parallel order path with its own tables, its own idempotency barrier,
its own execution listener and its own position ledger.

ADR 5 keeps `positions` and `manual_positions` separate. ADR 4 gives manual
orders their own two-phase commit.

**Consequence:** everything downstream became dual-path. Kill switch needed
scopes; reconciliation needed to unify two ledgers; PnL needed to hydrate both.
A large share of September's work is the system absorbing this change.

---

## 8. Scoped kill switch

**Dates:** 2026-09-15 (`0272637`), 2026-09-17 (`05cf74a`), migration
`l1m2n3o4p5q6`

From one kill switch per account to three scopes — engine, manual, account —
with the armed-uniqueness index widened from `(account_id)` to
`(account_id, scope)`.

**Consequence:** idempotency became scope-relative. Tests written against the
pre-scope model silently encoded the wrong semantics until 2026-09-21; see
[Chapter 10](../part2_safety_operations/10_kill_switch.md). This is the clearest
example in the repository of an architectural change outliving the tests that
described the old behaviour.

---

## 9. Centralized notifications

**Date:** 2026-09-17 · **Commit:** `42a36cd`

All alerting routed through one orchestrator with an intelligence layer.
Producers emit `NormalizedEvent`; nothing calls a channel directly.

**Consequence:** `send_canonical_telegram` became dead code retained only for
test-patch compatibility, which produced three misleading test failures in
Chapter 13. Suppression, dedup and flapping detection became possible because
there is a single choke point.

---

## 10. Operator audit trail

**Date:** 2026-09-18 · **Commit:** `5b9d094`, migration `a7u8d9i0t1r2`

From audit-over-`event_log` to a dedicated append-only `audit_events` table with
actor attribution, server-side `auth_sessions`, and the intent-first recorder.

**Consequence:** the system can now answer "who did this, from where, and what
was the state before and after" — which `event_log` structurally could not.
`event_log` remains for machine events.

---

## Reading the shape

Nine of these ten changes moved state or authority **toward a durable, single
owner**:

| From | To |
|---|---|
| Strategy holds broker logic | OMS owns submission, RMS owns safety |
| Code defines schema | Alembic defines schema |
| Two independent orders | One basket |
| Request executes inline | Request queues durably |
| In-process dedup set | `execution_claims` row |
| Bespoke process manager | systemd |
| Per-component alerting | One orchestrator |
| Audit derived from machine events | Purpose-built append-only table |

The exception is manual trading (#7), which deliberately *added* a parallel
path. It is also, measurably, the change that generated the most follow-up work.
That is not an argument against it — the capability was needed — but it is the
reason scopes, dual-ledger reconciliation and dual PnL hydration all exist.

**When you add a second way to do something, budget for every downstream
component that assumed there was one.**
