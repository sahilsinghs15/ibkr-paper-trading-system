# 03-DATA-OWNERSHIP.md — Data Ownership & Source of Truth

---

## 1. COMPREHENSIVE SOURCE-OF-TRUTH MATRIX

| Domain Concern | Authoritative Source | Primary Writer | Primary Readers | Derived Views / Caches | Classification |
|---|---|---|---|---|---|
| **Inbound Signals** | `signals` table | `OrderManager._persist_inbound_signal` | Webhook Ingest, WorkerPool | `signal_jobs.raw_payload` | Authoritative System Input |
| **Signal Queue Jobs** | `signal_jobs` table | `webhook_ingest.py`, `worker_pool.py` | `ExecutionWorkerPool`, `RedZoneReleaseService` | In-memory queue in WorkerPool | Authoritative Workflow State |
| **Execution Claims** | `execution_claims` table | `OrderManager._acquire_execution_claim` | OrderManager, RecoveryManager | In-memory `RMSContext.processed_signals` | Authoritative Dedupe Barrier |
| **Engine Orders** | `orders` table | `BasketCoordinator`, `IBKRExecutionAdapter` | OMS, Reconciler, OrderBook UI | In-memory `OMSOrder` objects | Authoritative Order Ledger |
| **Manual Orders** | `manual_orders` table | `ManualTradingService`, `ManualExecutionListener` | ManualTradePage, OrderBook UI | Frontend UI form state | Authoritative Manual Ledger |
| **Execution Fills** | IBKR Broker Socket | `IBKRExecutionAdapter`, `ManualExecutionListener` | Position Repositories, TradeBook UI | `executions`, `manual_executions`, `trade_executions` | External Truth (Broker Fill) |
| **Engine Positions** | `positions` table | `OrderManager._update_runtime_state` | PositionsPage, Reconciler, LivePnl | `demo_streaming` cache, UI view | Authoritative Pair Ledger |
| **Manual Positions** | `manual_positions` table | `ManualPositionRepository.apply_execution` | ManualPositionsPage, Reconciler | `demo_streaming` cache, UI view | Authoritative Lot Ledger |
| **Physical Broker Positions** | IBKR `reqPositions()` | `PositionReconciler.run_reconcile` | ReconcilePage, CriticalRecovery | `broker_positions` snapshot table | External Truth (Broker Snapshot) |
| **Market Prices (Marks)** | IBKR Market Data Stream | `LivePnlService.on_tick_price` | LivePnlService, PnL calculators | In-memory `LivePnlService._marks` | Ephemeral External Truth |
| **Unrealized P&L** | Derived Calculation | `LivePnlService._flush_pending` | UI Dashboard, demo_streaming | `positions.live_pnl`, `manual_positions.live_pnl` | Derived from Marks + Entry |
| **Realized P&L** | Position Ledgers (`realised_pnl`) | Position Repositories on reduction | UI Dashboard, Settings, Risk Monitors | Account loss monitors | Authoritative Derived (from Fills) |
| **Kill Switch State** | `kill_switch_operations` table | `KillSwitchService` | OrderManager, ManualTrading, UI | `_KILL_SWITCH_ACTIVE_ACCOUNTS` set | Authoritative Risk State |
| **Trading Pause State**| `accounts.trading_paused` | Config API (`routes/config.py`) | OrderManager, ManualTrading | `_TRADING_PAUSED_ACCOUNTS` set | Authoritative Operator Control |
| **AWS Daily Credits** | AWS Cost Explorer API | `InstanceCreditScheduler` | SystemMonitorPage | `instance_daily_credit_usage` table | External Truth (Cloud Billing) |

---

## 2. DATA CLASSIFICATION DEFINITIONS

1. **Authoritative State**:
   - Persisted in PostgreSQL.
   - Survives process crashes, restarts, and network drops.
   - Mutated only through designated repository methods under transaction control.
2. **External Truth**:
   - Originates from outside our database (IBKR gateway socket, AWS API).
   - Ingested through dedicated listeners and recorded into immutable ledgers.
   - When external truth contradicts local state, the difference is reconciled, never hidden.
3. **Derived Data**:
   - Calculated mathematically from authoritative and external state (e.g., unrealized PnL, Expected Net Inventory).
   - May be cached in Redis or in-memory sets, but can always be recomputed from scratch.
4. **Presentation / Display Data**:
   - Exists in React state, Zustand stores, or UI component props.
   - **MUST NEVER BE TRUSTED AS TRUTH BY BACKEND SERVICES**.

---

## 3. STATE MUTATION RULES

- **Never update positions without an execution fill**: Positions must only be created or modified when an authentic broker fill is received and deduplicated.
- **Never delete historical execution or order rows**: The audit trail must be append-only.
- **Never mutate cache directly without writing through to the database**: In-memory sets (like `_KILL_SWITCH_ACTIVE_ACCOUNTS`) must always be updated synchronously with their database rows.
