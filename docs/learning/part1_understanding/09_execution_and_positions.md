# Chapter 9 — Execution & Positions

## Current Implementation

### Responsibility

Turn broker fills into ledger truth: `executions` rows, position state, and
live PnL.

### Two position ledgers

Deliberately separate (ADR 5):

| Table | Grain | Written by |
|---|---|---|
| `positions` | Engine **pair** (leg A + leg B in one row) | `OrderManager._update_runtime_state` → `PositionRepository` |
| `manual_positions` | Manual **lot**, keyed by `trade_id` | `ManualExecutionListener` → `ManualPositionRepository` |

They are unified only at read time, in `PositionReconciler`. ADR 5 says not to
merge them without full migration planning: pair strategies need atomic
pair-level PnL and compensation; manual trading needs discrete lots.

### Execution recording

| Table | Contents |
|---|---|
| `executions` | Engine fills |
| `manual_executions` | Manual fills |
| `trade_executions` | Durable trade book, idempotent upsert |
| `broker_positions` | Snapshot from IBKR `position()` callbacks |

`exec_id` must be processed exactly once. Duplicate IBKR fill callbacks must not
double count — covered by `test_naked_pair_protection_fix.py` case 6.

### Live PnL

`LivePnlService` (`backend/app/services/pnl.py`) is the sole owner of real-time
marks and unrealized PnL. `positions.live_pnl` and `manual_positions.live_pnl`
are **derived** values, explicitly listed in `AGENTS.md` §5 as never
authoritative.

When a mark is unavailable the UI must show that state rather than a stale or
synthesized number (`003c6b9`, 2026-08-09, "Hide P&L when market_data_status is
UNAVAILABLE").

### Trade id rules

**A closed `trade_id` is never reused** (`AGENTS.md` §9). Enforced in the
manual path with an explicit rejection
(`MANUAL_POSITION_CLOSED_TRADE_ID_REUSE_REJECTED`) recorded in
`manual_audit_events`.

### Failure behavior

Row-level locking (`SELECT ... FOR UPDATE`) is required for position mutations.
Positions are never mutated directly at the broker — all changes originate from
validated orders.

### Current limitations

- Pair-row schema is Model Blue specific, not generic N-leg storage
  (noted in `position_repository.py`).
- The lifecycle review's P0 finding 1 (late `execDetails` regressing a terminal
  order) primarily manifests here, as an `orders` row with `status='CANCELLED'`
  and `fill_qty > 0` and no compensation sibling. **Status: not re-verified.**

---

## Engineering History

### Chronology

| Date | Change | Commit |
|---|---|---|
| 2026-08-18 | Broker executions persisted; fill precision; lifecycle audit events | `fd5f0d6` |
| 2026-08-18 | CFD share sizing to whole lots | `0d688a2` |
| 2026-08-20 | `opened_at` / `closed_at` timestamps on positions | `026df9c` |
| 2026-08-19 | Model Blue position sizing | `d9a9119` |
| 2026-09-03 | **Major fixes in position sizing** | `448a9e2` |
| 2026-09-10 | Background trade book sync; idempotent execution upsert | `6638176` |
| 2026-09-14 | Live PnL for manual positions | `00b2315`, `9b582f3` |
| 2026-09-15 | **Strict execution correlation prevents KIE→SMH mis-attachment** | `6130661` |
| 2026-09-15 | Repair mismatched executions causing "Qty 10 / Filled 30" | `fe44947` |

### The September 15 attribution fixes

Three commits on 2026-09-15 address the same underlying class of problem —
executions attaching to the wrong position:

- `fe44947` "repair mismatched executions causing Qty10 Filled30" — a position
  showing 10 ordered and 30 filled, i.e. fills from elsewhere landing on it.
- `6130661` "strict execution correlation prevents KIE→SMH mis-attachment" — a
  fill for one symbol attributed to another.
- `b977cad` "preserve close intent via trade_id, prevent new SHORT on CLOSE" —
  a CLOSE being treated as a new opening position.

There is also a standalone repair script,
`backend/scripts/repair_manual_mismatched_executions.py`, which establishes that
bad data reached the database and had to be corrected retroactively rather than
merely prevented going forward.

**Confidence: Confirmed** that these commits and the repair script exist.
**Unknown:** the precise trigger sequence, the number of affected positions, and
whether any financial impact occurred. The commit messages state the symptom;
available material does not record the investigation.

This is the strongest argument in the repository for `AGENTS.md` §9's rule:
**never correlate entities by symbol alone when stronger identities
(`con_id`, `trade_id`, `perm_id`, `exec_id`) exist.** The KIE→SMH
mis-attachment is precisely what symbol-based correlation produces.

### Case studies

A full case study is not written here because the investigation record does not
survive — only the commits and the repair script do. Writing one would require
inventing the diagnosis, which is exactly what this documentation must not do.
What *is* established is recorded above and in
[Chapter 17](../part3_engineering_history/17_data_integrity_fixes.md).
