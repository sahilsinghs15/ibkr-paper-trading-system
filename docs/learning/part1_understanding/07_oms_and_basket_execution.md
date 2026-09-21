# Chapter 7 — OMS & Basket Execution

## Current Implementation

### Responsibility

Get a multi-leg intent filled atomically enough that the account is never left
holding one side of a pair. When that cannot be achieved, unwind what did fill.

### Major components

| Component | File | Role |
|---|---|---|
| `OMSService` | `backend/app/oms/oms_service.py` | Submits individual legs |
| `BasketCoordinator` | `backend/app/oms/coordinator.py` | Basket lifecycle, retries, compensation |
| `Basket` / `BasketModel` | `backend/app/oms/basket.py` | Multi-leg state |
| `IBKRExecutionAdapter` | `backend/app/oms/ibkr_adapter.py` | Broker submission |

### Basket lifecycle

```text
EXECUTING → all legs filled            → ACCEPTED
          → incomplete after retries   → UNWINDING → COMPENSATED
          → unwind fails               → CRITICAL
```

### Retries — and what a "leg" means

This is the single most misunderstood part of the execution model, and it has
produced a UI bug (below) and continues to shape how signal data must be read.

`_retry_incomplete()` resubmits the **remaining quantity of an existing leg**.
It does not create a new leg. Concretely:

- Each retry creates a **new row in `orders`**.
- That row keeps the **same leg label** — `orders.leg` is `L0`, `L1`, … derived
  from `leg_index` (`coordinator.py`, `leg_label=f"L{order.leg_index ...}"`).
- The retry's `quantity` is the **remaining** amount, not the original.

So a two-leg pair that retried once has **three** rows in `orders`. Counting
order rows gives the wrong number of legs. The correct grouping, which the
backend performs in `reconcile_signal_status()`
(`backend/demo_streaming/snapshot.py`), is:

- key on `basket_id` + `leg`
- required quantity = **max** over the group (the original, not a remainder)
- filled quantity = **sum** over the group (cumulative across attempts)

Worked example from `backend/tests/test_naked_pair_protection_fix.py`:

| Order | leg | qty | filled | status |
|---|---|---|---|---|
| 1 | L0 | 399 | 300 | CANCELLED |
| 2 | L1 | 546 | 546 | FILLED |
| 3 | L0 | 99 | 99 | FILLED |

Two legs, not three. EWP required 399, filled 300 + 99 = 399. Basket is
complete → `ACCEPTED`, zero compensation orders.

### Retry events

`_retry_incomplete()` emits `AUTO_SQUARE_OFF_RETRY` on submission and
`AUTO_SQUARE_OFF_RETRY_BLOCKED` when RMS rejects the retry. Detail payload
carries `trade_id`, `symbol`, `leg`, `remaining_qty`, `retry`, `max_retries`,
and for submissions `broker_order_id`.

`max_retries` comes from operator-configurable `execution_settings`, not a
constant.

### Naked-pair compensation

If a basket cannot complete, `_compensate_filled()` emits reverse CLOSE legs for
what did fill. Compensation orders are marked `is_compensation=true` and are
excluded from primary-leg accounting.

### Failure behavior

Compensation only ever *reduces* exposure. A retry *adds* exposure. The two are
gated differently, and conflating them caused a defect fixed on 2026-09-21
(`64f3009`) — see Chapter 10.

### Current limitations

- **Finding 1 (P0, 2026-09-03, `BUGS-lifecycle.md`):** an `execDetails` arriving
  *after* a terminal `orderStatus` can regress a CANCELLED order to
  PARTIALLY_FILLED and leave it uncompensated, because `_compensate_filled`
  already read `filled_quantity`. **Status: not re-verified. Unknown.**
- **Finding C9 (P2):** the rate limiter does not prioritise order execution over
  market data, so leg 2 can time out while leg 1 is filled.

---

## Engineering History

### Chronology

| Date | Change | Commit |
|---|---|---|
| 2026-08-14 | OMS/RMS replace the strategy-based broker system | `902ca4d` |
| 2026-08-18 | `BasketModel`, `BasketRepository`, `BasketCoordinator` | `6bc05f4` |
| 2026-08-19 | Execution retry logic | `d62a8cd` |
| 2026-08-20 | Basket reconciliation for naked-pair protection | `7815be1` |
| 2026-08-20 | Truthful canonical signal reconciliation, EXPIRED handling | `8c99ea4` |
| 2026-09-03 | Lifecycle review: 4 P0 findings against the execution path | `448a9e2` |
| 2026-09-21 | Retry/leg grouping corrected in the UI; retry events enriched | `64f3009` |

### Naked-pair protection

`7815be1` (2026-08-20) introduced basket reconciliation for naked-pair
protection. `test_naked_pair_protection_fix.py` documents what it had to get
right, and the list is a good summary of the domain's sharp edges:

1. A retried leg (300 + 99 of 399) must reconcile to ACCEPTED with zero
   compensation — not be mistaken for an incomplete basket.
2. Retrying leg index 1 must keep `leg_index=1`.
3. The same symbol on two different legs must stay independent.
4. Genuine naked exposure must compensate for the net amount only.
5. A late cancellation callback arriving after a retry completed must not
   regress the basket.
6. Duplicate IBKR fill callbacks must not double count.

Items 1 and 3 are the same insight from opposite directions: **leg identity is
`leg_index`, not symbol, and not row count.**

---

# Case Study: Signal tray showed retries as extra legs

**Date:** 2026-09-21
**Feature:** OMS retries / Signal Tray UI
**Type:** Bug (presentation)
**Severity:** P2
**Status:** Fixed
**Related Components:** `BasketCoordinator._retry_incomplete`, `SignalTrayTable`, `SignalDetailModal`

---

## 1. What Was Happening?

When a leg had trouble filling, the Signal Tray and the signal detail drawer
displayed the retries as if they were additional legs — "LEG 1, LEG 2, LEG 3"
for what was actually a two-leg pair retried once.

Reported by the operator as logically wrong: the system was not processing three
legs, it was retrying the same leg.

## 2. Expected Behavior

A two-leg pair should render two legs, with retry progress shown as a retry
count.

## 3. Initial Understanding

The initial assumption was that this was purely a labelling problem — that the
UI had a retry counter and was simply rendering the wrong string, so the fix
would be to change the label.

That was wrong on both counts. There were two independent defects, and the
labelling one was the *less* important of the two.

## 4. Symptoms / Evidence

Code reading established both defects.

**Defect A — orders rendered one-to-one as legs.** In
`SignalDetailModal.tsx`:

```tsx
{primaryOrders.map((ord, idx) => (
  ...
  LEG {idx + 1} — {ord.symbol} ({ord.buy_sell})
))}
```

`primaryOrders` is every non-compensation order row. A retry adds a row, so a
retry adds a "LEG".

**Defect B — the retry counter could never fire.** Both components looked for:

```tsx
if (kind === 'BASKET_RETRY') {
  const attempt = detail.attempt ? ... : 'Retry'
```

A repository-wide search for that event kind:

```
$ grep -rn "BASKET_RETRY" --include=*.py .
(no matches)
```

The backend emits `AUTO_SQUARE_OFF_RETRY`, with the attempt number in
`detail.retry`, not `detail.attempt`. Both the kind **and** the field name were
wrong, so `latestAttempt` was always null and the `(Retry N/3)` suffix had never
rendered.

## 5. Investigation

### Step 1
Read `SignalTrayTable.computeFillSummary`. It mapped `primaryOrders` directly to
leg summaries — one entry per order row.

### Step 2
Checked whether retries produce new order rows. `coordinator.py`
`_retry_incomplete()` calls `submit_one_leg()` and appends to `created`, setting
`order.leg_index = index`. So yes: new row, same leg index.

### Step 3
Searched the backend for `BASKET_RETRY`. No match anywhere. Listed the event
kinds actually emitted: `AUTO_SQUARE_OFF_RETRY`,
`AUTO_SQUARE_OFF_RETRY_BLOCKED`, `BASKET_UNWINDING`. `BASKET_UNWINDING` *is*
emitted and the frontend matched it correctly — which is why the mismatch on the
retry kind was not obvious.

### Step 4
Found that the backend already solved the grouping problem correctly in
`reconcile_signal_status()` (`demo_streaming/snapshot.py`), keying on
`basket:{basket_id}:leg:{leg}` with `max` for required and `sum` for filled.
The frontend simply did not mirror it.

## 6. Root Cause

**Confirmed root cause A:** the UI treated `orders` rows as legs. Retries create
additional rows on the same logical leg, so a retried pair rendered as three
legs.

**Confirmed root cause B:** the frontend listened for an event kind
(`BASKET_RETRY`) and a detail field (`attempt`) that the backend has never
emitted. Retry progress therefore never displayed at all.

**Contributing factor:** the correct grouping logic already existed on the
backend but was not shared with or mirrored by the frontend, so the two sides
disagreed about what a leg is.

## 7. Code-Level Location

**Files:**
`frontend/src/components/SignalTrayTable.tsx`,
`frontend/src/components/SignalDetailModal.tsx`,
`backend/app/oms/coordinator.py`

**Functions:** `computeFillSummary()`, `buildUnifiedTimeline()`,
`computeRetryInfo()`, `BasketCoordinator._retry_incomplete()`

**Call path:**

```text
BasketCoordinator._retry_incomplete()
  → OMSService.submit_one_leg(retry_intent)      # new orders row, same leg_index
    → OrderRepository.upsert(leg_label="L{index}")
      → GET /demo/signals  → order_payload() → {"leg": "L0", ...}
        → SignalTrayTable.computeFillSummary()
          → primaryOrders.map(...)               # one entry per row  [defect A]
        → SignalDetailModal
          → kind === 'BASKET_RETRY'              # never matches      [defect B]
```

## 8. The Fix

**Frontend.** New shared module `frontend/src/utils/signalLegs.ts` mirroring
`reconcile_signal_status()`:

- `groupLogicalLegs(orders)` — keys on `basket:{basket_id}:leg:{leg}`, falls
  back to symbol+side when `leg` is absent; `req = max(...)`,
  `fill = sum(...)`, `retries = group size - 1`.
- `latestRetryAttempt(events, legs)` — reads `AUTO_SQUARE_OFF_RETRY` /
  `detail.retry`, falling back to per-leg retry count when events are absent.
- `formatRetryLabel(info)` — `"Retry 2/3"`, or `"Retry 2"` when the cap is
  unknown.

Both components now iterate logical legs.

**Backend.** `_retry_incomplete()` now includes `leg` and `max_retries` in both
retry event payloads, so the UI shows the operator-configured denominator
instead of a hardcoded `/3`. `max_retries` is configurable via
`execution_settings`, so the old hardcoded denominator could itself have been
wrong.

## 9. Verification

Eight unit tests in `frontend/e2e/unit/signalLegs.spec.ts`, including the exact
EWP/EWU case from the backend regression test, a case asserting the same symbol
on two distinct legs stays separate, and an explicit case asserting that legacy
`BASKET_RETRY` events are **not** counted.

Backend: `test_basket_retry.py`, `test_basket_coordinator.py`,
`test_naked_pair_protection_fix.py` — 40 passed.

## 10. Lessons Learned

- **Row count is not domain cardinality.** The `orders` table records
  *submissions*; the domain concept is a leg. Anything rendering or counting
  legs must group by `leg_index`.
- When two components must agree on a derivation, one of them should be the
  reference and the other should visibly mirror it. The backend had the correct
  logic for months; the frontend reimplemented it wrongly because nothing
  connected them.
- An event-name mismatch between producer and consumer fails *silently*. The
  adjacent kind (`BASKET_UNWINDING`) was correct, which made the broken one
  look plausible. A contract test over emitted event kinds would have caught it.

## 11. Open Questions

- When the `BASKET_RETRY` name diverged is **Unknown**. Whether it was ever
  emitted under that name is **not established** — no commit was traced.
- Whether any operator made a decision based on the phantom third leg is
  **Unknown**.
