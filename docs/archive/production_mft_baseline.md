# Production MFT Baseline & State Mutation Audit

This document establishes the exact read-only baseline for the current TradingView → FastAPI → OMS → IBKR execution pipeline prior to architectural refactoring.

---

## 1. End-to-End Control Flow & Signal Tracing

The execution workflow follows this direct synchronous path:

```
TradingView HTTP POST /api/webhooks/tradingview
  │
  ├── 1. Parsing & Webhook Capture
  │     ├── `receive_tradingview_webhook()` in `webhooks.py`
  │     ├── Synchronously writes raw JSON payload to disk (`data/tradingview_webhooks/webhook_*.json`)
  │     └── Invokes `order_manager.parse_inbound_payload()` -> `parse_tradingview_payload()`
  │
  ├── 2. Inbound Signal Persistence
  │     ├── `OrderManager.process_signal_execution()` in `order_manager.py`
  │     └── `OrderManager._persist_inbound_signal(signal, status="NEW")` -> `SignalRepository.record_inbound()` (PostgreSQL)
  │
  ├── 3. Strategy Routing & Account Fan-Out
  │     ├── `OrderManager._process_signal_execution_inner()`
  │     ├── Queries `DatabaseStrategyAccountRouter.resolve(strategy_id)` (DB lookup for enabled subscriptions)
  │     └── Loops **sequentially** over `contexts` (`_fanout_accounts()`):
  │           for ctx in contexts:
  │               handler.build_intent(signal, account=ctx)
  │               _evaluate_and_submit(intent, ...)
  │
  ├── 4. Sizing & RMS Evaluation
  │     ├── `ModelBlueStrategy.build_intent()` -> `ModelBlueSizer.size_legs()` (calculates leg quantities, reference prices)
  │     ├── `RMSEngine.evaluate(intent, rms_context)`
  │     │     ├── Check 2: Position limit (`max_open_positions`)
  │     │     ├── Check 3: Symbol money limit (`money_limit_per_symbol`)
  │     │     ├── Check 4: Duplicate signal check (`(account_id, strategy_id, trade_id)` in `rms_context.processed_signals`)
  │     │     ├── Check 7: Exposure limit (`symbol_exposures`)
  │     │     └── Check 8: Open position gate check (`is_open_blocked`)
  │     └── `OrderManager._audit_rms()` -> `EventRepository.append()` (persists `RMS_PASS` or `RMS_REJECT` event to PostgreSQL)
  │
  ├── 5. Instrument Resolution & CFD Discovery
  │     ├── `attach_resolved(intent)` -> `DatabaseInstrumentCatalog` lookup
  │     └── If CFD and conId unmapped: `ensure_cfd_instruments_for_symbols()`
  │           └── Calls `client.request_contract_details(contract, timeout=5.0)`
  │                 └── **BLOCKS FASTAPI EVENT LOOP** via `threading.Event().wait(timeout)` inside `tws_client.py`
  │
  ├── 6. OMS & Basket Execution
  │     ├── `BasketCoordinator.execute(intent, rms_result, ...)`
  │     │     ├── Sets shared instance attribute `self._active_basket = basket` (**MUTABLE SHARED STATE BUG**)
  │     │     ├── Persists `Basket` row (`BasketRepository.upsert()`)
  │     │     ├── Loop over legs: `OMSService.submit_one_leg()` -> `IBKRExecutionAdapter.submit_order()` -> `TWSClient.placeOrder()`
  │     │     ├── `_wait_terminals(submitted, timeout=90.0)`
  │     │     │     └── Calls `IBKRExecutionAdapter.wait_for_terminal_or_fill()`
  │     │     │           └── Awaits `asyncio.Future` for up to **90 seconds** per order leg!
  │     │     │
  │     │     ├── If incomplete:
  │     │     │     ├── Retry evaluation (`_retry_incomplete()`): cancels working orders, evaluates RMS for remaining leg qty, re-submits
  │     │     │     └── Unwind & Compensation (`_compensate_filled()`): if retries fail/expire, submits reverse order leg to square off filled legs
  │     │     └── Updates Basket state to `OPEN`, `CLOSED`, `COMPENSATED`, or `CRITICAL`
  │     │
  │  ├── 7. State Updates & Persistence
  │  │     ├── `OrderManager._update_runtime_state()`: updates `rms_context.processed_signals`, `open_positions`, `symbol_exposures`
  │  │     ├── `ModelBlueStrategy.after_submit()`: records trade in `ModelBlueTradeBook` and persists position row
  │  │     └── Updates signal row status to `PROCESSED` or `REJECTED` in PostgreSQL
  │  │
  │  └── 8. HTTP Response
  │        └── Returns `{"status": "received"}` or `{"status": "rejected_by_rms"}` to TradingView
```

---

## 2. Comprehensive Inventory of State Mutations

| Module | Location / Symbol | Mutation Type | Details / Concurrency Risk |
|---|---|---|---|
| `webhooks.py` | `WEBHOOK_CAPTURE_DIR` write | Filesystem (Sync) | `file_path.write_text(...)` blocks FastAPI event loop on disk I/O. |
| `webhooks.py` | Logger context | Thread-local | `bind_log_context()` sets request-scoped log context. |
| `order_manager.py` | `self._rms_context.processed_signals` | In-Memory `set` | Added to on signal processing (`add((account_id, strat_id, signal_id))`). Unenforced across process restarts without DB sync. |
| `order_manager.py` | `self._rms_context.open_positions` | In-Memory `dict` | Incremented/decremented on OPEN/CLOSE. |
| `order_manager.py` | `self._rms_context.symbol_exposures` | In-Memory `dict` | Updated on OPEN/CLOSE with calculated leg notionals. |
| `order_manager.py` | `self._rms_context.per_symbol_limits` | In-Memory `dict` | Reloaded from Postgres `per_symbol_limits` table. |
| `coordinator.py` | `self._active_basket` | In-Memory `Basket` | **CRITICAL RACE CONDITION**: Shared mutable field on singleton `BasketCoordinator`. Overwritten whenever a new basket executes concurrently. |
| `coordinator.py` | `self._order_baskets` | In-Memory `dict` | Maps `internal_order_id` to `Basket`. |
| `coordinator.py` | `self._critical` | In-Memory `set` | Tracks `(account_id, strategy_id)` pairs locked in CRITICAL state. |
| `coordinator.py` | `self._retry_ids` | In-Memory `set` | Deduplicates retry attempts by key string. |
| `ibkr_adapter.py` | `self._orders` | In-Memory `dict` | Maps `internal_order_id` to `OMSOrder`. |
| `ibkr_adapter.py` | `self._broker_map` | In-Memory `dict` | Maps `ibkr_order_id` to `internal_order_id`. |
| `ibkr_adapter.py` | `self._futures` | In-Memory `dict` | Maps `internal_order_id` to `asyncio.Future`. |
| `tws_client.py` | `self._contract_details_events` | In-Memory `dict` | Maps `reqId` to `threading.Event`. Uses blocking `.wait()`. |
| `tws_client.py` | `self.next_order_id` | In-Memory `int` | Incremented sequentially on order creation. |
| `SignalRepository` | `signals` table | Database (PostgreSQL) | Inserts/updates signal rows. Unique constraint on `(strategy_id, signal_id)`. |
| `BasketRepository` | `baskets` table | Database (PostgreSQL) | Upserts basket lifecycle records (`EXECUTING`, `OPEN`, `CLOSED`, `UNWINDING`, `COMPENSATED`, `CRITICAL`). |
| `OrderRepository` | `orders` table | Database (PostgreSQL) | Upserts child order records and broker order IDs. |
| `ExecutionRepository`| `executions` table | Database (PostgreSQL) | Appends fill execution records. |
| `PositionRepository` | `positions` table | Database (PostgreSQL) | Upserts position tracking records on open/close. |

---

## 3. Classification of Existing Functions: Business Logic vs Orchestration

To satisfy the **STRICT MANDATE** of changing **HOW** the system executes without altering **WHAT** decisions are made, functions are categorized as follows:

### A. Business Logic (MUST NOT BE MODIFIED IN SEMANTICS OR FORMULAS)

1. **`app/services/model_blue/sizer.py`**:
   - `ModelBlueSizer.size_legs()`: Pair sizing calculation, allocation leverage formulas, reference price leg sizing.
2. **`app/services/model_blue/strategy.py`**:
   - `ModelBlueStrategy.build_intent()`: Translates `Signal` into multi-leg pair `OrderIntent` (legs A and B).
   - `ModelBlueStrategy.after_submit()`: Post-fill bookkeeping for Model Blue trades.
3. **`app/rms/engine.py`**:
   - `RMSEngine.evaluate()`: Enforces Checks 2, 3, 4, 7, and 8. Returns `RMSOutcome.PASS` or `RMSOutcome.REJECT`.
4. **`app/rms/checks/*`**:
   - `Check2PositionLimit`, `Check3SymbolMoneyLimit`, `Check4DuplicateSignal`, `Check7ExposureLimit`, `Check8OpenPositionGate`: Concrete rule verification functions.
5. **`app/oms/retry_policy.py`**:
   - `ExecutionRetryPolicy`, `paper_retry_ports_allowed()`: Square-off/retry time windows and port validation.
6. **`app/oms/coordinator.py` (Business Semantics)**:
   - `_basket_complete()`: Determines if all legs met intended quantities within fill threshold (`_FILL_EPS`).
   - `_retry_intent()`: Constructs square-off leg for remaining unfilled quantity.
   - `_compensate_filled()`: Constructs reverse order intent to unwind partially filled legs on failure.

### B. Orchestration / Plumbing (TARGET FOR ARCHITECTURAL REFACTORING)

1. **`app/api/routes/webhooks.py`**:
   - `receive_tradingview_webhook()`, `_process_tradingview_webhook()`: Currently orchestrates disk writing and full in-line execution. **Will be converted to fast ingestion ACK only.**
2. **`app/services/order_manager.py`**:
   - `process_signal_execution()`, `_fanout_accounts()`, `_evaluate_and_submit()`: Orchestrates signal persistence, account iteration, RMS call, and OMS submission. **Will be called by asynchronous durable workers.**
3. **`app/oms/coordinator.py` (Orchestration Plumbing)**:
   - `BasketCoordinator.execute()`: Orchestrates child order dispatch, fill waiting loop, retry loop, and compensation loop. **Will be refactored to isolate per-basket state and eliminate `_active_basket`.**
4. **`app/broker/ibkr/tws_client.py`**:
   - `request_contract_details()`: Contract details request helper using `threading.Event().wait()`. **Will be wrapped/refactored to run off the FastAPI event loop.**
5. **`app/oms/ibkr_adapter.py`**:
   - Order submission, status routing, callback event listener, and fill future resolution. **Will interface with the centralized IBKR Execution Scheduler.**

---

## 4. Key Architectural Flaws Identified in Baseline

1. **Request-Bound Webhook Lifecycle**: Webhook holds HTTP connection open while waiting for IBKR fills (up to 90s + retries + compensation). TradingView timeouts (~10s) trigger automatic retries, causing duplicate concurrent executions.
2. **Synchronous I/O on Event Loop**:
   - Disk write `file_path.write_text()` in `webhooks.py`.
   - Thread blocking `threading.Event().wait()` in `tws_client.py` during CFD discovery.
3. **Shared Mutable Basket State**: `BasketCoordinator._active_basket` is overwritten when multiple signals execute concurrently, corrupting basket tracking.
4. **Sequential Account Fan-out**: Processing accounts sequentially in a single loop delays executions for downstream accounts if an upstream account waits for fills.
5. **Unbounded IBKR Requests**: Bursts of incoming signals directly spawn TWS API calls without concurrency control or pacing, risking TWS socket saturation or broker disconnects.
6. **In-Memory Volatile State**: Signals, RMS processed signal sets, and active basket tracking are lost on process crash, making recovery incomplete.

---
*Verified against active codebase.*
