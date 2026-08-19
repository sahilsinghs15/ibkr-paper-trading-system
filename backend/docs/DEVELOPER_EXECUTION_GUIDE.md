# Developer Execution Guide

> **Stale for agents:** Prefer [`../../AGENTS.md`](../../AGENTS.md) and [`../../docs/`](../../docs/). Several “not implemented” bullets below are outdated (for example account/position Postgres models and multi-account routing **do** exist in code). This file is kept as a human-oriented historical map; do not use it as the source of truth for what is implemented.

Practical architecture, navigation, and developer operations guide for the IBKR Paper Trading System backend.

---

## 1. What This System Currently Does

The system ingests external trading signals, validates them against business risk rules, tracks internal order lifecycles, and executes trades via IBKR Paper TWS over a TCP socket connection (`127.0.0.1:7497`).

```
External Signal (TradingView / Webhook)
      ↓
Signal Model (app/models/signal.py)
      ↓
OrderManager Facade (app/services/order_manager.py)
      ↓
OrderIntent Model (app/rms/models.py)
      ↓
RMS Engine (app/rms/engine.py & app/rms/checks/)
      ↓
OMS Service (app/oms/oms_service.py)
      ↓
IBKR Execution Adapter (app/oms/ibkr_adapter.py)
      ↓
TWS Client Transport (app/broker/ibkr/tws_client.py)
      ↓
IBKR Paper TWS (127.0.0.1:7497)
```

### Out of Scope (NOT IMPLEMENTED YET)
- Market data ingestion
- Internal strategy evaluation engines
- Account database & multi-account routing
- Position database & persistent position management
- Redis hot-state caching

---

## 2. Repository Map

```
backend/
├── app/
│   ├── api/                  # FastAPI routers and dependency injection
│   │   ├── deps.py           # State lookup helpers (get_oms, get_order_manager)
│   │   ├── router.py         # Main API v1 router aggregator
│   │   └── routes/
│   │       ├── health.py     # System health endpoint (/health)
│   │       ├── orders.py     # Order book inspection and cancellation (/api/v1/orders)
│   │       └── webhooks.py   # External signal ingestion (/api/webhooks/tradingview)
│   ├── broker/               # Low-level broker socket transport
│   │   └── ibkr/
│   │       └── tws_client.py # IBKR EClient/EWrapper TCP connector (127.0.0.1:7497)
│   ├── core/                 # Environment config & structured logging
│   │   ├── config.py         # Pydantic settings loading from .env
│   │   └── logger.py         # Application logger configuration
│   ├── models/               # Domain models
│   │   └── signal.py         # Signal and SignalType dataclass
│   ├── oms/                  # Order Management System
│   │   ├── ibkr_adapter.py   # Translates Order <-> IBKR Contract & IBOrder
│   │   ├── models.py         # Order, OrderStatus, ExecutionTimestamps, ExecutionResult
│   │   └── oms_service.py    # Order lifecycle owner & status state machine
│   ├── rms/                  # Risk Management System
│   │   ├── checks/           # Modular risk check implementations (Checks 2, 3, 4, 7, 8)
│   │   ├── engine.py         # RMS rule engine evaluator
│   │   └── models.py         # OrderIntent, OrderLeg, RMSContext, RMSResult
│   ├── schemas/              # Pydantic serialization contracts
│   │   ├── api_schemas.py    # Order API schemas
│   │   └── webhook.py        # Webhook payload schemas
│   └── services/             # Application orchestration
│       └── order_manager.py  # Application facade (Signal -> OrderIntent -> RMS -> OMS)
├── docs/
│   └── DEVELOPER_EXECUTION_GUIDE.md # This guide
├── scripts/
│   └── oms/
│       └── run_paper_execution.py  # Developer Paper TWS acceptance script
└── tests/                    # Unit and integration pytest suite
```

---

## 3. "WHERE DO I GO IF I WANT TO..."

| Developer Task | File Location |
| :--- | :--- |
| **Change RMS Check 2 (Duplicate Signal)** | [app/rms/checks/duplicate.py](file:///home/dev3/Documents/ibkr-paper-trading-system/backend/app/rms/checks/duplicate.py) |
| **Change RMS Check 3 (Strategy Authorization)** | [app/rms/checks/strategy.py](file:///home/dev3/Documents/ibkr-paper-trading-system/backend/app/rms/checks/strategy.py) |
| **Change RMS Check 4 (Contract Month & Rollover)** | [app/rms/checks/contract_month.py](file:///home/dev3/Documents/ibkr-paper-trading-system/backend/app/rms/checks/contract_month.py) |
| **Change RMS Check 7 (Open Position Limit)** | [app/rms/checks/position_limit.py](file:///home/dev3/Documents/ibkr-paper-trading-system/backend/app/rms/checks/position_limit.py) |
| **Change RMS Check 8 (Money Per Stock Limit)** | [app/rms/checks/money_per_stock.py](file:///home/dev3/Documents/ibkr-paper-trading-system/backend/app/rms/checks/money_per_stock.py) |
| **Change RMS Pipeline Execution Order** | [app/rms/engine.py](file:///home/dev3/Documents/ibkr-paper-trading-system/backend/app/rms/engine.py) |
| **Change `OrderIntent` or `OrderLeg` Model** | [app/rms/models.py](file:///home/dev3/Documents/ibkr-paper-trading-system/backend/app/rms/models.py) |
| **Change Internal Order Lifecycle State Machine** | [app/oms/oms_service.py](file:///home/dev3/Documents/ibkr-paper-trading-system/backend/app/oms/oms_service.py) |
| **Change `Order` or `ExecutionTimestamps` Model** | [app/oms/models.py](file:///home/dev3/Documents/ibkr-paper-trading-system/backend/app/oms/models.py) |
| **Change IBKR Order/Contract Translation** | [app/oms/ibkr_adapter.py](file:///home/dev3/Documents/ibkr-paper-trading-system/backend/app/oms/ibkr_adapter.py) |
| **Change Low-Level TWS Socket transport** | [app/broker/ibkr/tws_client.py](file:///home/dev3/Documents/ibkr-paper-trading-system/backend/app/broker/ibkr/tws_client.py) |
| **Change Webhook Signal Ingestion** | [app/api/routes/webhooks.py](file:///home/dev3/Documents/ibkr-paper-trading-system/backend/app/api/routes/webhooks.py) |
| **Change REST API Order Endpoints** | [app/api/routes/orders.py](file:///home/dev3/Documents/ibkr-paper-trading-system/backend/app/api/routes/orders.py) |
| **Change Application Startup/Shutdown Lifespan** | [app/main.py](file:///home/dev3/Documents/ibkr-paper-trading-system/backend/app/main.py) |

---

## 4. Complete Order Flow

1. **Signal Ingestion**: `POST /api/webhooks/tradingview` receives JSON payload and constructs `Signal`.
2. **OrderManager Ingestion**: `OrderManager.process_signal(signal)` receives `Signal`.
3. **Intent Creation**: `OrderManager` translates `Signal` into an `OrderIntent` containing an `OrderLeg`.
4. **RMS Check Evaluation**: `RMSEngine.evaluate(intent)` runs active rules in order:
   - Check 2: Duplicate signal check
   - Check 3: Strategy validation check
   - Check 4: Contract month & rollover check
   - Check 7: Open position limit check
   - Check 8: Money per stock limit check
5. **RMS PASS**: If all checks return `PASS`, `RMSEngine` returns an `RMSResult` with `outcome = PASS`.
6. **OMS Creation**: `OrderManager` calls `OMSService.submit_intent(intent, rms_result)`. `OMSService` instantiates an internal `Order` in state `PENDING`.
7. **IBKR Order Translation**: `OMSService` calls `IBKRExecutionAdapter.submit_order(order)`. Adapter converts `Order` into IBKR `Contract` and `IBOrder`.
8. **TWS Socket Transmission**: Adapter calls `TWSClient.placeOrder(tws_id, contract, ib_order)`. `TWSClient` sends byte stream to `127.0.0.1:7497`.
9. **TWS Acknowledgment**: IBKR TWS returns `openOrder` and `orderStatus` callbacks. Adapter updates `Order.status = SUBMITTED` and records `order_status_received_at`.
10. **Execution / Fill**: When paper fill occurs, TWS sends `execDetails` and `commissionReport` callbacks. Adapter updates `filled_quantity`, `average_fill_price`, `execution_received_at`, and sets `status = FILLED`.

---

## 5. Component Ownership

- **`TWSClient`**: Owns low-level IBKR API socket transport (`127.0.0.1:7497`). Manages `EClient`/`EWrapper` socket reader thread and forwards callbacks to listeners. Contains **no business rules**.
- **`IBKRExecutionAdapter`**: Owns IBKR API translation. Maps `Order` to IBKR `Contract` and `IBOrder`, maps IBKR order status strings to `OrderStatus`, handles `execDetails` and `commissionReport` callbacks, and resolves async wait futures.
- **`OMSService`**: Owns internal order lifecycle state (`Order`). Manages timestamp instrumentation, order lookup, and cancellation.
- **`OrderManager`**: Owns application orchestration. Coordinates `Signal` $\to$ `OrderIntent` $\to$ `RMSEngine` $\to$ `OMSService`.
- **`RMSEngine`**: Owns risk evaluation logic. Evaluates Checks 2, 3, 4, 7, 8 against `OrderIntent`.

---

## 6. Models Explained

| Model | Module | Purpose | Created By | Modified By | Consumed By |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`Signal`** | `app/models/signal.py` | Trading signal | Ingestion Router | Immutable | `OrderManager` |
| **`OrderIntent`** | `app/rms/models.py` | Pre-execution sized instruction | `OrderManager` | RMS Checks (if ADJUST) | `RMSEngine`, `OMSService` |
| **`RMSResult`** | `app/rms/models.py` | Risk check evaluation summary | `RMSEngine` | Immutable | `OMSService` |
| **`Order` / `OMSOrder`** | `app/oms/models.py` | Single internal order model | `OMSService` | `IBKRExecutionAdapter` | `OMSService`, API Routes |
| **`ExecutionTimestamps`** | `app/oms/models.py` | Boundary latency timestamps | `Order` | `OMSService`, `IBKRExecutionAdapter` | Execution Reports |

---

## 7. Callback Flow

```
TWS API Callback
      ↓
TWSClient (EWrapper)
      ↓
IBKRExecutionAdapter (Listener)
      ↓
Order State & Timestamp Updates (in OMSService)
```

- **`orderStatus`**: Maps IBKR status (`PreSubmitted`, `Submitted`, `Cancelled`, `Filled`, `Inactive`) to `OrderStatus`.
- **`execDetails`**: Captures `execId`, `shares`, `price`, `cumQty`, `avgPrice`, and updates `filled_quantity` and `execution_received_at`.
- **`commissionReport`**: Logs IBKR execution commission details associated with `execId`.
- **`on_error`**: Distinguishes informational status warnings (e.g., Code 10349 TIF preset notice) from real order rejections (e.g., Code 201, 202).

---

## 8. Testing Guide

### A. Offline Unit & Integration Tests (No TWS Required)
Run the automated pytest suite using mocked TWS connections:
```bash
.venv/bin/pytest
```

### B. Code Formatting & Linting Check
```bash
.venv/bin/ruff check app/ tests/ scripts/
```

### C. Live Developer Paper TWS Acceptance Test
> **WARNING**: THIS SCRIPT SUBMITS A REAL PAPER ORDER TO YOUR LOCAL PAPER TWS SESSION.
```bash
.venv/bin/python scripts/oms/run_paper_execution.py
```

---

## 9. Configuration

Configuration settings are loaded via Pydantic in [app/core/config.py](file:///home/dev3/Documents/ibkr-paper-trading-system/backend/app/core/config.py) from environment variables or `.env`:

| Environment Variable | Default Value | Purpose |
| :--- | :--- | :--- |
| `IBKR_HOST` | `127.0.0.1` | Local TWS/Gateway IP address |
| `IBKR_PORT` | `7497` | TWS Paper Trading TCP port |
| `IBKR_CLIENT_ID` | `1` | API Client ID |
| `IBKR_CONNECTION_TIMEOUT` | `10.0` | Connection handshake timeout in seconds |
| `TRADING_SYMBOL` | `RELIANCE` | Default trading symbol |
| `ORDER_QUANTITY` | `1` | Default order quantity |
| `LOG_LEVEL` | `INFO` | Logging output level |

---

## 10. How To Debug A Failed Order

Follow this step-by-step diagnostic checklist:
1. **Is TWS Connected?**: Check application startup logs for `TWS connection established and handshake completed`.
2. **Did `OrderManager` receive the Signal?**: Check logs for `BUY/SELL signal received — submitting order`.
3. **Did RMS PASS?**: Check logs for `RMS Result: PASS`. If rejected, look for `RMS check X rejected intent: reason`.
4. **Did OMS create the Order?**: Check logs for `OMS created internal order ORD-...`.
5. **Did `IBKRExecutionAdapter` submit?**: Check logs for `Submitting order to IBKR TWS: tws_id=X`.
6. **Did TWS acknowledge?**: Look for `ANSWER openOrder` and `ANSWER orderStatus` callbacks from IBKR.
7. **Did order receive fills?**: Look for `IBKR execDetails callback: exec_shares=X`.

---

## 11. Common Developer Mistakes

1. **Running Acceptance Script Unintentionally**: `run_paper_execution.py` submits actual paper orders. Only run it intentionally when Paper TWS is active.
2. **"Read-Only API" Enabled in TWS**: If TWS returns `Error 321`, navigate to TWS `Edit -> Global Configuration -> API -> Settings` and **uncheck** `Read-Only API`.
3. **Wrong Socket Port**: Live TWS uses `7496`; Paper TWS uses `7497`. Ensure port `7497` is configured.
4. **Duplicate Client ID**: Using the same `client_id` across multiple running processes causes IBKR to disconnect the earlier session.
5. **Putting RMS Logic in OrderManager**: Keep `OrderManager` as a pure facade. Put risk checks inside `app/rms/checks/`.
6. **Putting Business Logic in `TWSClient`**: Keep `TWSClient` strictly as a low-level socket transport wrapper.

---

## 12. How To Add A New RMS Check

To add a new RMS check (e.g., Check 9):
1. **Create Check File**: Create `app/rms/checks/check_name.py` containing a check function:
   ```python
   def check_something(intent: OrderIntent, context: RMSContext) -> CheckResult:
       # Return CheckResult(check_number=9, check_name="...", outcome=RMSOutcome.PASS)
   ```
2. **Register in Engine**: Import and add `check_something` to `RMSEngine._run_checks()` in `app/rms/engine.py`.
3. **Add Unit Test**: Create `tests/rms/test_rms_check_name.py` verifying `PASS` and `REJECT` scenarios.

---

## 13. How To Modify IBKR Execution

- **To modify IBKR Contract/IBOrder translation**: Edit `_build_contract()` or `_build_ib_order()` in `app/oms/ibkr_adapter.py`.
- **To modify IBKR socket transport or callbacks**: Edit `app/broker/ibkr/tws_client.py`.
- **To modify internal order state transitions**: Edit `OMSService` in `app/oms/oms_service.py`.

---

## 14. Future Work (NOT IMPLEMENTED YET)

The following architectural components belong to future project phases and are **NOT** currently implemented:
- [ ] Production external signal schema contract
- [ ] Persistent Signal Database
- [ ] Persistent Order Database
- [ ] Account Database & Multi-Account Routing
- [ ] Position Database & Persistent Position Reconciliation
- [ ] Redis Hot-State Cache
- [ ] Live Production Multi-Broker Support
