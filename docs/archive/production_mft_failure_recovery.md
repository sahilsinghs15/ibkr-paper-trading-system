# Production MFT Failure Recovery & Crash Resilience

This document outlines the crash recovery matrix, state reconciliation protocol, and worker lease reclamation mechanism.

---

## 1. Crash Recovery Matrix

| Scenario | State at Crash Time | Recovery Action on Process Restart / Worker Reclamation | Resulting State |
|---|---|---|---|
| **1. Worker crash before order submission** | Job `CLAIMED` / `PROCESSING`, Basket row not created in DB. | Lease expires. Recovery manager re-claims job. Evaluates RMS and submits orders fresh. | `COMPLETED` or `REJECTED` |
| **2. Worker crash after order submission** | Job `PROCESSING`, Basket `EXECUTING`, child orders in DB with `ibkr_order_id`. | Recovery manager queries TWS open orders & executions (`reqAllOpenOrders`). Reconciles order fills from broker snapshot. Avoids duplicate order placement. | `OPEN` or `CLOSED` or `UNWINDING` |
| **3. Process crash while waiting for fills** | Job `PROCESSING`, Basket `EXECUTING`, child orders submitted to IBKR. | On restart, system requests broker execution snapshot. Matches fills against DB orders. Resumes fill wait or transitions basket. | `OPEN` or `UNWINDING` |
| **4. Process crash during square-off retry** | Job `PROCESSING`, Basket `EXECUTING`, retry orders submitted. | Reconciles retry orders via broker snapshot. Re-evaluates leg completeness. | `OPEN` or `UNWINDING` |
| **5. Process crash during compensation** | Job `PROCESSING`, Basket `UNWINDING`, compensation orders submitted. | Reconciles compensation orders via broker snapshot. Completes square-off. | `COMPENSATED` or `CRITICAL` |
| **6. IBKR Gateway disconnect** | Network connection dropped during active execution. | Adapter detects disconnect (`on_disconnected`). Pauses worker submission until reconnect. Re-synchronizes open orders on reconnect. | Paused -> Resumed |
| **7. Database temporarily unavailable** | Ingestion or worker DB query fails. | Webhook returns 503 retryable. Workers retry DB query with exponential backoff. No state corrupted. | Retried safely |
| **8. Sudden power off / SIGKILL restart** | Multiple jobs in `CLAIMED`/`PROCESSING` states. | `RecoveryManager.run_startup_recovery()` executes on application boot before worker pool starts. | Clean state restored |

---

## 2. Broker Reconciliation Protocol

When the application restarts or recovers a non-terminal job:

```
                          Application Boot / Recovery Scan
                                         │
                                         ▼
                     Find jobs in [CLAIMED] or [PROCESSING]
                     Find baskets in [EXECUTING] or [UNWINDING]
                                         │
                                         ▼
                        Connect to IBKR TWS Gateway
                                         │
                                         ▼
                        Request Open Orders & Executions
                    (`reqAllOpenOrders()` + `reqExecutions()`)
                                         │
                   ┌─────────────────────┴─────────────────────┐
                   │                                           │
         Broker snapshot matches DB                 Broker snapshot missing
       (Orders exist on Gateway)                   (No orders on Gateway)
                   │                                           │
                   ▼                                           ▼
       Update order statuses & fills               Check if submission occurred.
       Check leg completeness.                    If not submitted: Re-submit intent.
       Resume fill wait or square-off.            If unconfirmed: Mark RECOVERY_REQUIRED.
```

---

## 3. Worker Lease Expiration & Stale Job Reclamation

To ensure dead workers do not leave jobs stranded in `CLAIMED` state:

1. **Worker Heartbeat**: Active worker tasks update `lease_expires_at = NOW() + INTERVAL '30 seconds'` every 10 seconds on claimed jobs.
2. **Reclamation Task**: A background monitor runs every 15 seconds executing:
   ```sql
   UPDATE signal_jobs
   SET status = 'QUEUED',
       worker_id = NULL,
       claimed_at = NULL,
       lease_expires_at = NULL,
       attempt_count = attempt_count + 1
   WHERE status = 'CLAIMED'
     AND lease_expires_at < NOW()
     AND attempt_count < max_attempts;
   ```
3. **Dead Letter Escalation**: If a job exceeds `max_attempts` (e.g., 3 attempts), it transitions to `DEAD_LETTER` status for operator inspection, and an operational event is logged.

---
*Failure Recovery Specification.*
