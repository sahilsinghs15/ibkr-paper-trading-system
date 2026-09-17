# 06-EXECUTION-LIFECYCLE.md — Execution Fills & Callback Processing

---

## 1. INBOUND EXECUTION FLOW (IBKR $\rightarrow$ POSITION)

```
[IBKR Gateway Socket]
       │
       ▼
[TWSClient Reader Thread]
       │
       ├─► 1. `execDetails(reqId, contract, execution)`
       │      └─► Dispatches to `ManualExecutionListener` and `IBKRExecutionAdapter`
       │
       └─► 2. `commissionReport(commissionReport)`
              └─► Dispatches to listeners (may arrive before or after execDetails)
```

---

## 2. MANUAL EXECUTION PROCESSING (`ManualExecutionListener`)

All manual execution callbacks pass through `handle_exec_details()`:

```
1. Thread-Safe Dispatch:
   Dispatches coroutine onto the running asyncio event loop.

2. Out-of-Order Commission Retrieval:
   Pops any commission previously buffered in `_pending_commissions[raw_exec_id]`.

3. Strict Order Correlation:
   Matches the execution to a `manual_orders` record using strict filters:
   - Match by `orderRef` (`internal_order_id`)
   - Verify contract matches (`conId`, `symbol`, `secType`)
   - Verify `permId` matches if present
   - Verify `ibkr_account` matches
   (Strictly forbids symbol-only fallback to prevent cross-symbol mis-attachment).

4. Guard Against CLOSED trade_id Reuse:
   Checks if the target `manual_positions` row has `status == 'CLOSED'` and `signed_qty == 0`.
   If closed, the execution is REJECTED to prevent resurrecting a dead lot.

5. Idempotent Execution Insertion:
   Calls `ManualExecutionRepository.record_execution_with_dedup()`.
   Enforces unique constraint `uq_manual_executions_exec_id`.
   If the fill was already recorded:
   - Logs `MANUAL_EXECUTION_DUPLICATE` audit event.
   - Ignores duplicate callback without touching the position.

6. Atomic Position Mutation:
   Calls `ManualPositionRepository.apply_execution()`.
   Locks row with `SELECT ... FOR UPDATE`.
   Applies one of the 8 position lifecycle transitions.
   Updates `avg_cost` on additions; computes `realized_pnl` on reductions.

7. Order Status Update:
   Calculates total filled quantity across all fills for the order.
   Updates `manual_orders.status` to `FILLED` or `PARTIALLY_FILLED`.
   (Preserves `CANCELLED` status if late fill).

8. Trade Book Mirroring:
   Upserts fill line into unified `trade_executions` table.

9. Live PnL Watch Trigger:
   If position is now `OPEN`: calls `LivePnlService.watch_open()`.
   If position is now `CLOSED`: calls `LivePnlService.unwatch()`.
```

---

## 3. OUT-OF-ORDER COMMISSION HANDLING

At IBKR, the `commissionReport` callback frequently arrives asynchronously:
- **Case A: Commission arrives AFTER `execDetails`**:
  - `handle_commission_report()` queries `manual_executions` by `exec_id`.
  - Row exists: updates commission and decrements `manual_positions.realized_pnl`.
- **Case B: Commission arrives BEFORE `execDetails`**:
  - `handle_commission_report()` queries `manual_executions` by `exec_id`.
  - Row does NOT exist yet: buffers commission in `self._pending_commissions[raw_exec_id]`.
  - When `handle_exec_details()` subsequently executes, it pops the buffered commission and applies it atomically in the primary position mutation.

---

## 4. EXECUTION INVARIANTS

1. **At-Most-Once Position Application**:
   - An execution fill with a specific `exec_id` must mutate a position ledger **exactly once**.
2. **Strict CFD Contract Correlation**:
   - Fills must not be attached to orders based on symbol alone. An order for `SMH` must never attach an execution for `KIE` regardless of broker order ID overlap.
3. **No Phantom Fills**:
   - Position quantities must only change when corroborated by an authentic `execDetails` record.
