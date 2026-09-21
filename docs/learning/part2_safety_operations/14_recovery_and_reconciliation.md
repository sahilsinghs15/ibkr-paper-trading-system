# Chapter 14 — Recovery & Reconciliation

## Current Implementation

### Responsibility

Detect and explain disagreement between the ledger and the broker, and bring the
system back to a known state after a restart or failure.

`PositionReconciler` is the sole owner of reconciliation and anomaly detection
(`AGENTS.md` §4).

### Components

| Component | File |
|---|---|
| `PositionReconciler` | `backend/app/services/position_reconciler.py` |
| Diff classification | `backend/app/services/reconcile_service.py` |
| `RecoveryManager` | `backend/app/services/recovery.py` |
| Watchdog | `backend/scripts/watchdog_main.py` |
| Tables | `broker_positions`, `position_reconcile_runs`, `event_log` |

### Diff kinds

Each sweep compares broker lines against ledger lines and classifies:

| Kind | Meaning | Audit action when fixed |
|---|---|---|
| `QTY_DRIFT` | Quantities disagree | `INVENTORY_FIX_QUANTITY_MISMATCH` |
| `LEDGER_GHOST` | Ledger has it, broker does not | `INVENTORY_FIX_LEDGER_GHOST` |
| `BROKER_ORPHAN` | Broker has it, ledger does not | `INVENTORY_FIX_BROKER_GHOST` |

A sweep logs `broker_lines`, `match`, `ghost`, `orphan`, `drift`, `unmapped`,
`timed_out` and `error`.

### Rogue confirmation

A mismatch must persist for `rogue_confirm_sweeps` consecutive sweeps before it
alerts. Single-sweep noise and in-flight mismatches never alert — covered by
`test_rogue_skips_in_flight_and_one_sweep_noise`. Resolution fires only when the
mismatch is fully gone, not merely in flight.

Detection appends `ROGUE_TRADE_DETECTED` / `ROGUE_TRADE_RESOLVED` to
`event_log` with an idempotency key, then notifies via the orchestrator.

### Account isolation

Reconciliation is strictly per account. Manual inventory in one account never
affects another — `test_strict_account_isolation_in_reconciliation`.

### Startup recovery

`RecoveryManager.run_startup_recovery()` runs at boot. Kill-switch caches are
rehydrated from the database (`hydrate_kill_switch_cache`), which is why the
release predicate and the hydration predicate must match (Chapter 10).

### Failure behavior

Reconciliation is read-and-report by default. Fixes are operator-initiated and
audited with their specific fix type (Chapter 12).

### Current limitations

- Ghost counts in the development database run into the hundreds
  (`ghost=131` observed 2026-09-21) because test data accumulates. This is a
  dev-environment artifact, not a production signal, but it makes the dev
  reconcile log hard to read.
- The flapping/rogue windows are in-memory and do not survive a restart.

---

## Engineering History

### Chronology

| Date | Change | Commit |
|---|---|---|
| 2026-08-21 | Historical kill-switch position repair script | `323d138` |
| 2026-09-01 | Market-closed state handling in watchdog; backend-only manual restart | `4a4d2d8` |
| 2026-09-01 | Watchdog safety-gate flapping fixed; service health separated from trading safety | `f8a9a91` |
| 2026-09-01 | False readiness degradation resolved; transient timeouts debounced | `423f65a` |
| 2026-09-15 | Rogue trade spam fix | `460637e` |
| 2026-09-16 | Kill-switch eventual convergence, row-level locking | `29fce78` |
| 2026-09-17 | Rogue tests isolated after merge | `ddc63fc` |

### Separating health from safety

`f8a9a91` (2026-09-01) is titled "prevent watchdog safety gate flapping and
separate service health from trading safety". The distinction it draws is worth
keeping in mind whenever touching the watchdog:

- **Service health** — is the process up and responding?
- **Trading safety** — is it safe to trade right now?

Conflating them produces both false alarms (a healthy service reported unsafe)
and, worse, false confidence. The same day, `94ce7b6` resolved a watchdog
safety-gate HTTP 401 — the gate had been failing for an *authentication* reason
and that was being read as a safety signal.

This is the same shape as the worker heartbeat lesson in Chapter 4: **"could not
determine" is not the same as "determined to be false"**, and a system that
conflates them will either cry wolf or go quiet at the wrong moment.

### Repair scripts as historical evidence

Two scripts exist under `backend/scripts/`:

- `repair_historical_killswitch_positions.py` (2026-08-21)
- `repair_manual_mismatched_executions.py` (2026-09-15)

Their existence establishes something the commit messages alone do not: in both
cases bad data reached the database and had to be corrected retroactively.
Prevention was added at the same time, but it did not reach back.

`backend/tests/test_repair_historical_killswitch_positions.py` covers the first.

### Case studies

The rogue-alert test migration is in
[Chapter 13](13_notifications.md), since its root cause was the notification
pipeline change rather than reconciliation.
