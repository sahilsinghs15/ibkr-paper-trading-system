# 16-WORKERS-SCHEDULERS.md — Background Workers & Schedulers

---

## 1. INVENTORY OF BACKGROUND TASKS

All background tasks are spawned during `app/main.py` lifespan startup and registered on `app.state`:

| Service / Worker | Lifecycle Entrypoint | Schedule / Frequency | Purpose |
|---|---|---|---|
| **`ExecutionWorkerPool`** | `worker_pool.py` | Continuous (10 concurrent tasks) | Claims queued signal jobs from `signal_jobs` and executes them. |
| **`PositionReconciler`** | `position_reconciler.py` | Every 30.0 seconds | Sweeps broker vs ledger positions and logs diffs. |
| **`RiskExitMonitor`** | `risk_exit_monitor.py` | Every 2.0 seconds (if enabled) | Evaluates open pair stops/targets and account loss limits. |
| **`LossThresholdMonitor`**| `loss_threshold_monitor.py` | Every 30.0 seconds | Scans enabled accounts for cumulative realized loss breaches. |
| **`TradeBookSyncService`**| `trade_book_sync_service.py`| Every 10.0 seconds | Polls IBKR for current-day execution fills per account. |
| **`RedZoneReleaseService`**| `red_zone_release.py` | Every 60.0 seconds | Re-queues deferred jobs when the next RTH session opens. |
| **`InstanceCreditScheduler`**| `instance_credit/scheduler.py` | Daily at ~08:00 UTC | Queries AWS Cost Explorer API for previous UTC day cost. |
| **`CriticalRecoveryService`**| `critical_recovery.py` | Event-driven / On-startup | Background auto-flattener for `BASKET_CRITICAL` incidents. |

---

## 2. WORKER CONCURRENCY & DOMAIN LOCKING

In `ExecutionWorkerPool`:
- **Worker Leases**: Workers set `lease_expires_at = now() + 30s` and status `PROCESSING` on claimed jobs.
- **Domain Locks**: Each worker acquires an in-memory lock on `(account_scope, strategy_id)` before processing a job to prevent concurrent execution races across the same partition.
- **Reclaimer Task**: Runs every 15 seconds to reclaim abandoned or expired leases from dead workers.

---

## 3. GRACEFUL SHUTDOWN ORDER

When `app.main:app` terminates, `lifespan()` executes cleanup in strict reverse order:
1. Stop `instance_credit_scheduler`.
2. Stop `loss_monitor`.
3. Stop `trade_book_sync`.
4. Stop `risk_exit_monitor`.
5. Stop `red_zone_release`.
6. Stop `margin_scanner`.
7. Stop `account_margin`.
8. Stop `position_reconciler`.
9. Stop `worker_pool` (waits for active in-flight jobs to finish).
10. Stop `critical_recovery`.
11. Disconnect `TWSClient` socket cleanly via `client.disconnect_clean()`.
