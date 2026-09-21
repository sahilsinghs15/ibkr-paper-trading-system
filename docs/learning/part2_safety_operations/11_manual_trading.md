# Chapter 11 — Manual Trading

## Current Implementation

### Responsibility

Let an operator place and cancel orders directly, with the same durability and
audit guarantees as engine orders, and without the two paths corrupting each
other's ledgers.

### Ownership

| Concern | Owner |
|---|---|
| Order submission | `ManualTradingService` → `ManualOrderRepository` |
| Fills and position mutation | `ManualExecutionListener` (`manual_callbacks.py`) → `ManualPositionRepository` |

`AGENTS.md` §4 makes `ManualExecutionListener` the **exclusive** owner of manual
fills and position mutation. Nothing else may write `manual_positions`.

### The idempotency barrier

Two-phase commit (ADR 4). The `manual_orders` row is inserted with status
`PENDING_SUBMIT` and **committed** before `placeOrder()` is called. The unique
constraint is `(account_id, idempotency_key)` —
`uq_manual_orders_account_idempotency`.

The concurrent race is handled explicitly. On `IntegrityError`, the service
rolls back, re-reads by idempotency key, and compares the full parameter set:

- same parameters → idempotent replay, returns the existing order, **no second
  broker order**
- different parameters → HTTP 409

Parameters compared: `con_id`, `symbol`, `side`, `quantity`, `order_type`,
`limit_price`, `tif`, `exchange`, `currency`, `sec_type`, and `trade_id` when
supplied.

Because the commit precedes `placeOrder()`, a loser of the unique-constraint
race can never place an order.

### Trade id rules

A closed `trade_id` is never reused. Attempts are rejected and recorded in
`manual_audit_events` as
`MANUAL_POSITION_CLOSED_TRADE_ID_REUSE_REJECTED`.

### Manual halt

`manual_halt_state` and the `_MANUAL_KILL_SWITCH_ACTIVE_ACCOUNTS` cache block
manual submission while a manual flatten is in flight. See Chapter 10 for the
defect where that block outlived the operation.

### What blocks the manual ticket

`validate_pretrade()` is the single gate; `preview_order()` and `submit_order()`
both run it, so the UI disables Submit *and* the server re-rejects at 400.

| Gate | Blocks | Reducing-order exemption |
|---|---|---|
| Gateway disconnected / account disabled | all | no |
| `manual_halt_state.halted` | all | no |
| Kill switch, `account` scope | all | no |
| Kill switch, `manual` scope | all | no |
| Kill switch, `engine` scope | **nothing** | n/a |
| `accounts.trading_paused` | opens | **yes** — an order that genuinely reduces an open lot is permitted |

Engine scope is absent by design (Chapter 10, scope-isolation case study). Note
the asymmetry in the last column: trading pause lets the operator close, the
kill switch does not. That is deliberate — pause is a soft stop, an armed kill
switch means the flatten owns the position.

Two things are **not** gated and remain available under every scope:
cancelling a resting manual order (`cancel_order()` does not call
`validate_pretrade()`), and the engine pair-close endpoint. Both only reduce
risk.

### Out-of-order callbacks

`commissionReport` can precede `execDetails`; `_pending_commissions` buffers
them. Preserve it (Risk 5, `18-KNOWN-RISKS.md`).

### Current limitations

- `test_concurrent_same_idempotency_exactly_one_placeorder` failed **once** in
  a full-suite run on 2026-09-21 and could not be reproduced in 20+ isolated
  runs or 3 per-file runs. The barrier was re-read and is correct. Recorded as
  an unexplained rare flake in
  [Chapter 21](../part4_ownership/21_current_known_limitations.md) rather than
  closed.

---

## Engineering History

### Chronology

| Date | Change | Commit |
|---|---|---|
| 2026-09-11 | Manual trading system: persistence, API, frontend | `837baf2` |
| 2026-09-14 | Prevent closed trade-id reuse; remove STOP order type | `2c74e1b` |
| 2026-09-14 | CFD `eTradeOnly` error, rejected status update, margin SKIPPED semantics | `383c4b0` |
| 2026-09-14 | Idempotency UUID lifecycle; expanded parameter comparison; hide internal keys | `d7be7a8` |
| 2026-09-14 | Idempotency fallback for insecure HTTP (`crypto.randomUUID` unavailable) | `8b4c6c1` |
| 2026-09-15 | Preserve close intent via `trade_id`; prevent new SHORT on CLOSE | `b977cad` |
| 2026-09-15 | Repair mismatched executions ("Qty 10 / Filled 30") | `fe44947` |
| 2026-09-15 | Strict execution correlation prevents KIE→SMH mis-attachment | `6130661` |
| 2026-09-16 | Pagination for manual orders and positions | `f5b5ef9` |
| 2026-09-18 | Unified Manual Trading workspace with inline ledger | `cd2b22a`, `5b52382` |

### The idempotency key lifecycle

Three commits on 2026-09-14 concern the idempotency key, and together they show
the feature being hardened from both ends:

- `d7be7a8` fixed the **lifecycle** (when the key is generated and reset),
  expanded the parameter comparison, and hid internal keys from the UI.
- `8b4c6c1` added a fallback because `crypto.randomUUID` is unavailable over
  insecure HTTP — a browser API that only exists in a secure context, which
  would have failed silently on a plain-HTTP deployment.
- `4aa0724` excluded the frontend test file from the `tsc` build.

The second is a good reminder that the client half of an idempotency scheme is
part of the safety mechanism. A key that fails to generate is a key that cannot
deduplicate.

### Why manual and engine ledgers stay separate

ADR 5. Pair strategies need atomic pair-level PnL and compensation; manual
trading needs discrete lots keyed by `trade_id`. They are unified only in
`PositionReconciler` at read time. Merging them is explicitly gated on full
migration planning.

The September 15 attribution fixes (see
[Chapter 9](../part1_understanding/09_execution_and_positions.md)) are the
strongest evidence for keeping them apart: even with separate ledgers, fills
were landing on the wrong positions. A merged schema would have made that
harder to detect, not easier.

### Case studies

The manual-path defects of 2026-09-14/15 are recorded in
[Chapter 17](../part3_engineering_history/17_data_integrity_fixes.md). Full case
studies are not written for them because the investigation record does not
survive — only commit messages, the regression tests, and a repair script.
