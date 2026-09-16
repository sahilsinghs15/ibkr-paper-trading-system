# 02-CODE-STRUCTURE.md — Code Organization & Logic Placement

---

## 1. LOGIC PLACEMENT MATRIX

When adding or modifying functionality, place code strictly in accordance with this directory and responsibility map:

| Type of Logic | Correct Directory / Layer | Example Classes | Forbidden Locations |
|---|---|---|---|
| **HTTP Request Parsing & Auth** | `app/api/routes/`, `app/api/deps.py` | `ManualOrderSubmitRequest`, `require_authenticated_user` | Services, Repositories |
| **Pre-Trade Gate Checks** | `app/services/` | `ManualTradingService.validate_pretrade` | Routes, Repositories, UI |
| **Algorithmic Risk Checks** | `app/rms/checks/` | `MarginCheck`, `MoneyPerStockCheck` | Routes, Services, Adapters |
| **Order Orchestration** | `app/services/`, `app/oms/` | `OrderManager`, `BasketCoordinator` | API Routes, Repositories |
| **Broker Transmission** | `app/broker/ibkr/`, `app/oms/` | `TWSClient`, `IBKRExecutionAdapter` | Services, Routes |
| **Position & Fill Accounting** | `app/db/repositories/`, `app/services/` | `ManualPositionRepository`, `ManualExecutionListener` | API Routes, UI |
| **Database Queries & Mutations** | `app/db/repositories/` | `OrderRepository`, `EventRepository` | API Routes, RMS Checks |
| **ORM Entity Mapping** | `app/db/models/` | `OrderModel`, `PositionModel` | Repositories, Schemas |
| **Contract Resolution** | `app/instruments/` | `InstrumentResolver`, `DatabaseInstrumentCatalog` | Services, UI |
| **Real-Time PnL Calculation** | `app/services/` | `LivePnlService` | API Routes, Repositories, UI |
| **User Interface Presentation** | `frontend/src/pages/`, `src/components/` | `ManualTradePage`, `PositionsPage` | Backend |

---

## 2. MAJOR SERVICE CLASSES & RESPONSIBILITIES

### `OrderManager` (`app/services/order_manager.py`)
- **Responsibility**: Top-level execution manager for engine signals.
- **Dependencies**: `OMSService`, `RMSEngine`, `RMSContext`, `CommittedCapitalProvider`, `ModelBlueStrategy`, `BasketCoordinator`.
- **Public Methods**: `process_signal_execution()`, `hydrate_runtime_from_db()`, `rebuild_rms_from_positions()`.
- **Invariants**: Must acquire exposure locks (`_exposure_guard`) before RMS evaluation; must commit `execution_claims` row before submitting to OMS.

### `ManualTradingService` (`app/services/manual_trading.py`)
- **Responsibility**: Pre-trade validation, what-if margin probes, durable two-phase commit submissions for manual CFD orders.
- **Dependencies**: `AsyncSession`, `TWSClient`, `IBKRExecutionAdapter`, `ManualOrderRepository`, `ManualPositionRepository`.
- **Public Methods**: `validate_pretrade()`, `preview_order()`, `submit_order()`, `cancel_order()`.
- **Invariants**: Strict two-phase commit: writes `PENDING_SUBMIT` to PostgreSQL and commits *before* placing order with IBKR.

### `ManualExecutionListener` (`app/services/manual_callbacks.py`)
- **Responsibility**: Handles IBKR callbacks (`orderStatus`, `openOrder`, `execDetails`, `commissionReport`) for manual orders.
- **Dependencies**: `async_sessionmaker[AsyncSession]`, `TWSClient`, `LivePnlService`.
- **Public Methods**: `handle_order_status()`, `handle_open_order()`, `handle_exec_details()`, `handle_commission_report()`.
- **Invariants**: Dispatches from thread to event loop; correlates by contract + perm + orderRef; enforces unique `exec_id`; locks position row `with_for_update()`.

### `BasketCoordinator` (`app/oms/coordinator.py`)
- **Responsibility**: Orchestrates atomic multi-leg pair orders. Tracks fill timeouts and triggers compensation on partial fills.
- **Dependencies**: `OMSService`, `AsyncSessionLocal`, `CriticalRecoveryService`.
- **Public Methods**: `execute()`, `is_open_blocked()`.
- **Invariants**: Enters `BASKET_CRITICAL` if compensation fails; blocks subsequent `OPEN` orders on that strategy/account.

### `LivePnlService` (`app/services/pnl.py`)
- **Responsibility**: Subscribes to IBKR market data ticks and computes real-time mark prices and unrealized PnL.
- **Dependencies**: `AsyncSessionLocal`, `TWSClient`, `GatewayRateLimiter`.
- **Public Methods**: `watch_open()`, `unwatch()`, `on_tick_price()`.
- **Invariants**: Never uses entry price as mark; throttles PostgreSQL writes to $\ge 1.0$s interval.

### `PositionReconciler` (`app/services/position_reconciler.py`)
- **Responsibility**: 30-second background audit comparing Expected Net Inventory (Engine + Manual) to broker physical snapshot.
- **Dependencies**: `AsyncSessionLocal`, `TWSClient`, `BrokerPositionRepository`.
- **Public Methods**: `run_reconcile()`, `start()`, `stop()`.
- **Invariants**: Requires 2 consecutive sweeps before emitting rogue trade alerts to Telegram.

---

## 3. PROHIBITED CROSS-LAYER CALLS

1. **Routes MUST NOT call `TWSClient` directly**: Orders must pass through `ManualTradingService` or `OrderManager`.
2. **RMS Checks MUST NOT write to the Database**: RMS checks are pure in-memory evaluators of `OrderIntent` against `RMSContext`.
3. **Broker Callbacks MUST NOT execute long DB transactions on the reader thread**: All callback handlers must dispatch coroutines to the asyncio event loop.
4. **Repositories MUST NOT call Services**: Repositories are pure data access components.
