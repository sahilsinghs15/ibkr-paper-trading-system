# Chapter 22 — Important Invariants

Things that must never break. Each entry names what enforces it and, where the
history records one, what happened when it did break.

## Execution

| # | Invariant | Enforced by | Broken once? |
|---|---|---|---|
| 1 | Exactly one `TWSClient` socket | `AGENTS.md` §6.1; single construction in `main.py` | — |
| 2 | Every outbound broker call passes `GatewayRateLimiter.acquire()` | `AGENTS.md` §6.2 | — |
| 3 | Engine orders commit an `execution_claims` row **before** submitting | ADR 3; `ExecutionClaimRepository` | Finding C2 — three paths bypass it. **Unknown** |
| 4 | Manual orders commit `manual_orders` **before** `placeOrder()` | ADR 4; `uq_manual_orders_account_idempotency` | — |
| 5 | RMS checks 1,2,3,4,7,8,101 run before every engine order | `AGENTS.md` §6.5 | — |
| 6 | Order ids come from `allocate_next_order_id()` | `AGENTS.md` §7 | — |
| 7 | Red Zone gates all non-emergency orders | `SessionClock`; tested since 2026-09-21 | Gate correct; **tests** depended on wall clock (Ch. 6) |

## Identity

| # | Invariant | Why |
|---|---|---|
| 8 | A closed `trade_id` is **never** reused | Lot identity; enforced with explicit rejection |
| 9 | `exec_id` is processed exactly once | Duplicate fills would double-count |
| 10 | Never correlate by symbol alone when `con_id`/`trade_id`/`perm_id` exist | KIE→SMH mis-attribution, 2026-09-15 |
| 11 | `broker_order_id` is session-scoped; `perm_id` is permanent | Survives gateway restarts |
| 12 | CFD `con_id` ≠ underlying STK `con_id` | Market data uses the underlying |
| 13 | `account_id` (BIGINT) is never sent to IBKR; `ibkr_account` (string) is | Type confusion broke reject reasons, `c943f87` |

## State

| # | Invariant | Enforced by | Broken once? |
|---|---|---|---|
| 14 | Completing a flatten does **not** disarm the kill switch | ADR 6 | — |
| 15 | Armed kill-switch uniqueness is per `(account_id, scope)` | `uq_kill_switch_operations_armed_account_scope` | Finding C1 — was check-then-act. **Fixed** |
| 16 | Every in-memory cache is reconstructable from the database | `hydrate_kill_switch_cache` | Manual cache release disagreed with hydration. **Fixed 2026-09-21** |
| 16a | A kill-switch scope blocks only the ledger it flattens: `engine`→engine, `manual`→manual, `account`→both | `is_account_kill_switch_active` / `is_manual_trading_blocked`; `test_kill_switch_scope_isolation.py` | Engine and account shared one cache, so engine scope blocked manual trading. **Fixed 2026-09-21** |
| 16b | A broker snapshot quantity is only compared with live ledger state when `broker_positions.as_of` post-dates the action being evidenced and the symbol has not moved since | `_reconcile_and_finalize_manual`; `test_kill_switch_manual_reconcile_race.py` | Cross-instant compare could mark a live lot `FLATTENED_PENDING_PRICE`. **Fixed 2026-09-21** |
| 17 | Position mutations use `SELECT ... FOR UPDATE` | `AGENTS.md` §8 | — |
| 18 | No broker call inside an open DB transaction | `AGENTS.md` §8 | Related to C10 pool exhaustion. **Unknown** |
| 19 | `audit_events` is append-only | `audit_events_guard()` trigger | — |
| 20 | Operator actions record intent **before** the side effect | `recorder.operation()` | — |
| 21 | Audit is committed before `trading-backend` stops or restarts | `SELF_UNIT` + `required=True` | — |

## Domain

| # | Invariant | Why |
|---|---|---|
| 22 | A retry is a new `orders` row on an **existing** leg | Leg identity is `leg_index`, not row count (Ch. 7) |
| 23 | Leg grouping: `req = max(...)`, `fill = sum(...)` per `basket_id`+`leg` | A retry carries the *remaining* quantity |
| 24 | Compensation only ever **reduces** exposure; a retry **adds** | Why compensation runs during a kill switch, retries do not |
| 25 | Marks are never invented; entry price is never a mark | Risk 3; fall back mid → previous close, else show unavailable |
| 26 | The frontend is presentation only | `AGENTS.md` §11 — never trust its state or validations |

## How to use this list

Before changing something that looks over-engineered, search this table. Most of
these exist because the opposite behaviour caused a real problem, and the
"Broken once?" column tells you where to read about it.

Where the column says **Unknown**, the invariant is asserted but its current
enforcement has not been re-verified — see
[Chapter 21](21_current_known_limitations.md).
