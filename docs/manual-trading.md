# Manual Trading Architecture & Specification

## 1. Overview & Objective

Manual Trading provides discretionary execution capability in the IBKR trading and execution system. Unlike algorithmic trading driven by TradingView webhook alerts and Model Blue sizing, manual trading is operator-driven directly from the UI.

### Scope by Milestone

- **M0 (Complete)**: Documentation stub, frontend routing, navigation entry, structured empty shell (`ManualTradePage.tsx`), and separate manual positions page (`ManualPositionsPage.tsx`).
- **M0.5 (Complete)**: PostgreSQL persistence schema (`manual_*` tables), SQLAlchemy models, repositories (CRUD & query only), Alembic migration (`w4x5y6z7a8b9`), API contracts/schemas, and unit/integration tests with explicit source isolation. **ZERO broker writes.**
- **M1-A (Complete)**: Safe Gateway status endpoint (`GET /api/v1/manual/gateway-status`), Paper/Live runtime detection via managed account prefixes (`VERIFIED_PAPER`, `VERIFIED_LIVE`, `UNKNOWN`), CFD instrument discovery (`POST /api/v1/manual/instruments/search`) using single `TWSClient` + `GatewayRateLimiter` (zero auto-picking, all candidates returned), explicit contract selection UI (`Selected Contract` card with con_id), zero broker writes.
- **M1-B (Complete)**: Environment-agnostic pre-trade validation, non-mutating preview (`POST /api/v1/manual/orders/preview`), margin what-if probe, durable idempotent order submission (`POST /api/v1/manual/orders`), two-phase commit (`PENDING_SUBMIT` committed to PostgreSQL prior to broker call), order ID allocation via `tws_client.allocate_next_order_id()`, broker order placement via single `TWSClient` and `GatewayRateLimiter` (`PRIORITY_ORDER_EXECUTION`), transition to `SUBMITTED`, audit logging (`manual_audit_events`), and frontend preview/confirmation modal with double-submit protection. **Zero fill ingestion (M1-C).**
- **M1-C (Complete)**: Broker callback ingestion (`orderStatus`, `openOrder`, `execDetails`, `commissionReport`), manual order correlation, durable execution persistence (`manual_executions`), exactly-once deduplication via DB constraint on `exec_id`, atomic position ledger mutation in `ManualPositionRepository.apply_execution()`, realized P&L calculation (with commission deduction), restart safety and ambiguous `PENDING_SUBMIT` recovery without automated resubmission.
- **M1-D (Complete)**: Extended inventory & reconciliation awareness. Aggregates Engine Net Quantity + Manual Net Quantity across `(account_id, normalized_symbol, normalized_sec_type)` without combining DB ledgers. Updated `PositionReconciler`, `ReconcileDiff`, Reconcile API (`collect_reconcile_positions`), and `BrokerAlignService` target calculations to include open manual positions. In-flight manual order tracking for reconciler sync skip. Zero broker order writes.
- **M1-E (Complete)**: Manual order cancellation (`POST /api/v1/manual/orders/{order_id}/cancel`), callback state machine hardening, permanently terminal `FILLED` invariant, reconnect resurrect protection, out-of-order execution recording without lifecycle regression, persistent order querying via polling, and frontend interactive cancellation with strict account isolation. Zero rogue broker order writes.

---

## 2. Hard Invariants & Broker-Write Safety

1. **Environment-Agnostic Manual Trading**:
   - Manual Trading is **NOT** hardcoded or restricted to Paper trading.
   - It talks to whichever IBKR environment the deployment is configured to use (Live Gateway 4001, Paper Gateway 4002, TWS, etc.).
   - Gateway environment classification (`VERIFIED_PAPER`, `VERIFIED_LIVE`, `UNKNOWN`) is strictly for operational visibility, diagnostics, UI display, and deployment verification.
   - It is **NOT** a business-rule gate (`if environment != VERIFIED_PAPER: reject_order()` is strictly forbidden).

2. **First-Class Manual Identity**:
   - Manual orders, executions, and positions permanently carry:
     `source = "manual"`
   - Engine trades carry:
     `source = "engine"`
   - Manual orders reside exclusively in `manual_orders`. Fake engine orders (`orders` table), fake `signal_id`, fake `strategy_id`, or `execution_claims` entries are strictly forbidden.

3. **Single TWS Gateway Infrastructure**:
   - Exactly one `TWSClient` instance (`fastapi_app.state.client`) and one socket connection.
   - Zero secondary sockets or clients.
   - All broker-mutating calls pass through the shared in-process `GatewayRateLimiter` at `PRIORITY_ORDER_EXECUTION` (= 1).

4. **Two-Phase Durable Idempotency Barrier**:
   - Database constraint `UNIQUE(account_id, idempotency_key)` is authoritative.
   - Order submission flow:
     1. Server-side pre-trade revalidation.
     2. Idempotency check against `manual_orders`.
        - If existing key with identical parameters: return existing order without calling broker.
        - If existing key with conflicting parameters: reject with HTTP 409 Conflict without calling broker.
     3. Insert `PENDING_SUBMIT` record and **COMMIT** transaction to PostgreSQL.
     4. Acquire `GatewayRateLimiter` token (timeout = 5.0s).
     5. Allocate broker order ID via `tws_client.allocate_next_order_id()`.
     6. Call `tws_client.placeOrder(broker_order_id, contract, ib_order)`.
     7. Update record to `SUBMITTED` with `broker_order_id` and committed to PostgreSQL.
     8. Record audit event in `manual_audit_events`.
   - **Critical**: `PENDING_SUBMIT` is committed before `placeOrder()`. If placeOrder raises, the order is updated to `ERROR`. An order is **NEVER** falsely reported as `SUBMITTED`.

5. **SUBMITTED State Semantics**:
   - `SUBMITTED` means the order was accepted by the local TWSClient socket.
   - `SUBMITTED` does **NOT** mean `FILLED`.
   - Orders are **NEVER** marked `FILLED` in M1-B. Fills belong strictly to broker execution callbacks in M1-C.

6. **No Automatic Retry After Ambiguity**:
   - If `placeOrder()` was called but a subsequent crash or database failure leaves the state ambiguous, the system **NEVER** blindly resubmits.
   - Ambiguous orders remain in `PENDING_SUBMIT` or `ERROR` for operator review or reconciliation in later milestones.

7. **Supported M1-B Order Types**:
   - Supported for broker execution: `MARKET` and `LIMIT`.
   - `STOP` orders may exist in persistence schemas but are explicitly **rejected** if submitted to IBKR in M1-B.
   - `outsideRth = False` is hardcoded server-side; operator or client input cannot override this.

8. **Pre-Trade Risk & Safety Validations**:
   - **Account Authorization**: Caller must be authorized for `account_id` / `ibkr_account`.
   - **Gateway Connection**: Broker client must be actively connected (`is_connected() == True`).
   - **Account Enabled**: If `account.enabled` is false, preview and submit are blocked.
   - **Manual Halt**: If `manual_halt_state.halted` is true, preview and submit are blocked.
   - **Kill Switch**: If `kill_switch_operations` is active (`ACTIVATING` or `ACTIVE`), preview and submit are blocked.
   - **Trading Pause**: If `account.trading_paused` is true, `OPEN` orders are rejected. Only valid `CLOSE` orders (opposing an open manual position up to current signed quantity) are permitted.
   - **Contract Validation**: Requires resolved contract (`con_id > 0`, `symbol`, `secType == 'CFD'`, `exchange`, `currency`, `minTick`). Auto-selection is forbidden.
   - **Quantity Validation**: Must be positive (`quantity > 0`).
   - **Price & minTick Validation**: For `LIMIT`, price > 0 and price must conform to contract `minTick`.

---

## 3. API Contracts (M1-B)

### 3.1 Non-Mutating Order Preview
`POST /api/v1/manual/orders/preview?ibkr_account={ibkr_account}`

**Request Body**:
```json
{
  "symbol": "IBUS500",
  "con_id": 10001,
  "sec_type": "CFD",
  "exchange": "SMART",
  "currency": "USD",
  "side": "BUY",
  "quantity": "10",
  "order_type": "LIMIT",
  "limit_price": "5200.25",
  "intent": "OPEN",
  "min_tick": 0.25
}
```

**Response Body**:
```json
{
  "valid": true,
  "account_id": 1,
  "ibkr_account": "DU123456",
  "environment": "VERIFIED_PAPER",
  "gateway_connected": true,
  "symbol": "IBUS500",
  "con_id": 10001,
  "side": "BUY",
  "quantity": "10",
  "order_type": "LIMIT",
  "limit_price": "5200.25",
  "effective_price": "5200.25",
  "notional": "52002.50",
  "init_margin_change": "1500.00",
  "maint_margin_change": "750.00",
  "margin_status": "AVAILABLE",
  "errors": [],
  "warnings": []
}
```

### 3.2 Durable Order Submission
`POST /api/v1/manual/orders?ibkr_account={ibkr_account}`

**Request Body**:
```json
{
  "idempotency_key": "man_order_c8f1a23e90b",
  "symbol": "IBUS500",
  "con_id": 10001,
  "sec_type": "CFD",
  "exchange": "SMART",
  "currency": "USD",
  "side": "BUY",
  "quantity": "10",
  "order_type": "LIMIT",
  "limit_price": "5200.25",
  "intent": "OPEN",
  "min_tick": 0.25
}
```

**Response Body**:
```json
{
  "order_id": 42,
  "internal_order_id": "MAN_9F2B8D3A01E2",
  "broker_order_id": 5001,
  "account_id": 1,
  "ibkr_account": "DU123456",
  "symbol": "IBUS500",
  "con_id": 10001,
  "side": "BUY",
  "quantity": "10",
  "order_type": "LIMIT",
  "limit_price": "5200.25",
  "status": "SUBMITTED",
  "created_at": "2026-09-11T16:00:00Z",
  "submitted_at": "2026-09-11T16:00:00.120Z",
  "source": "manual",
  "is_duplicate": false
}
```

---

## 4. Position Grouping & Uniqueness Strategy

In `manual_positions`, the uniqueness constraint is:
```sql
CONSTRAINT uq_manual_positions_account_trade UNIQUE (account_id, trade_id)
```

- Each manual trade intent is initiated under a distinct `trade_id` (e.g. `MAN_TRD_20260911_001`).
- Multiple manual positions for the **same symbol** can coexist if they are opened under **different `trade_id`s**.
- Within a single `trade_id`, fills in M1-C will accumulate or reduce the position for that trade.

---

## 5. Frontend Preview/Confirmation Flow

1. User selects account and verifies Gateway status.
2. User searches CFD contracts and explicitly selects a contract candidate.
3. User selects Side (`BUY` / `SELL`), Order Type (`MARKET` / `LIMIT`), Quantity, and Limit Price.
4. User clicks **Preview Order**:
   - Non-mutating `POST /orders/preview` runs full server-side validation and what-if margin probe.
   - UI opens **Order Confirmation & Risk Preview** modal displaying: Account, Environment, Gateway state, Contract con_id, Side, Quantity, Order Type, Price, Notional, Margin Impact, and any validation warnings.
5. User clicks **Confirm & Submit**:
   - Double-submit protection disables buttons while in-flight.
   - Client sends unique idempotency key.
   - Server returns order with status `SUBMITTED` and broker order ID.
   - UI alerts operator with order confirmation details (never optimistically marked `FILLED`).

---

## 6. Milestone M1-C: Broker Callbacks, Ingestion, and Position Ledger

Milestone M1-C implements inbound broker callback ingestion, exactly-once execution handling, atomic position ledger mutations, realized P&L calculations, commission reconciliation, and restart ambiguity recovery.

### 6.1 Single TWSClient Callback Architecture
There is strictly **one** `TWSClient` socket connection shared across the entire application (`app.state.client`).
- `ManualExecutionListener` is attached to `TWSClient` via `client.add_listener(manual_listener)`.
- It receives the following callbacks multiplexed across accounts:
  - `orderStatus(orderId, status, filled, remaining, avgFillPrice, permId, parentId, lastFillPrice, clientId, whyHeld, mktCapPrice)`
  - `openOrder(orderId, contract, order, orderState)`
  - `execDetails(reqId, contract, execution)`
  - `commissionReport(commissionReport)`
- Callbacks invoked on the background IBKR reader thread are safely marshaled to the running `asyncio` event loop via `asyncio.run_coroutine_threadsafe()`.

### 6.2 Manual Order Correlation Strategy
Callbacks are correlated to manual orders through a multi-tier hierarchy:
1. **Tier 1 — `broker_order_id`**: Matches `manual_orders.broker_order_id` (cast to string).
2. **Tier 2 — `perm_id`**: Matches `manual_orders.perm_id` (durable broker permanent ID).
3. **Tier 3 — `orderRef` / `internal_order_id`**: During M1-B submission, `orderRef` is stamped with `internal_order_id`. If received in `openOrder` or `execution`, matches `manual_orders.internal_order_id`.
4. **Verification**: In all cases, the broker account (`acctNumber`) must strictly match `manual_orders.account_id` / `ibkr_account`.
5. **No False Correlations**: If correlation fails, the callback is logged as unresolved (`MANUAL_EXECUTION_UNRESOLVED`). It is **never** attached to an engine order or an arbitrary manual order based solely on symbol.

### 6.3 Exactly-Once Execution Ingestion Guarantee
Execution callbacks from IBKR can be duplicated, delayed, replayed, or re-sent across reconnects.
- **Durable Uniqueness**: `manual_executions.exec_id` has a database `UNIQUE` constraint.
- **Atomic Mutation Boundary**:
  - In `ManualExecutionListener.handle_exec_details()`, a single database transaction opens:
    ```python
    async with session.begin():
        # 1. Insert execution record using ON CONFLICT DO NOTHING (or check existence)
        exec_model, is_new = await exec_repo.record_execution_with_dedup(...)
        if is_new:
            # 2. Mutate manual_positions via apply_execution()
            await pos_repo.apply_execution(...)
            # 3. Append to broker-wide Trade Book (trade_executions) with order_id=None
            await trade_exec_repo.record_fill(...)
            # 4. Log audit event
            await audit_repo.record_event(...)
    ```
  - If `is_new` is `False`, the duplicate callback is ignored without double-mutating positions, realized P&L, or commission.
  - Concurrency tests prove that N concurrent duplicate callbacks produce exactly 1 execution record and exactly 1 position mutation.

### 6.4 Commission Timing & Handling
IBKR sends `commissionReport` separately from `execDetails`.
- **Case A: `commissionReport` arrives after `execDetails`**:
  `ManualExecutionRepository.update_commission()` updates `manual_executions.commission`. If the execution closed or reduced a position, `ManualPositionRepository.apply_execution()` deducts the commission from `manual_positions.realized_pnl` deterministically.
- **Case B: `commissionReport` arrives before `execDetails`**:
  An in-memory pending commission buffer (`_pending_commissions[exec_id]`) holds the report until `execDetails` arrives, attaching it immediately during execution persistence.

### 6.5 Position Ledger Math & Realized P&L
Position mutations in `manual_positions` strictly maintain `account_id + trade_id`:
1. **Open Long**: Initial BUY when `signed_qty == 0`. `signed_qty = +qty`, `avg_cost = price`.
2. **Add Long**: Additional BUY when `signed_qty > 0`. `new_qty = existing + qty`, `avg_cost = ((existing_qty * avg_cost) + (qty * price)) / new_qty`.
3. **Reduce Long**: SELL when `qty < existing_signed_qty`. `signed_qty = existing - qty`. `realized_pnl += qty * (price - avg_cost) - commission`. `avg_cost` remains unchanged.
4. **Close Long**: SELL when `qty == existing_signed_qty`. `signed_qty = 0`, `status = "CLOSED"`. `realized_pnl += qty * (price - avg_cost) - commission`.
5. **Open Short**: Initial SELL when `signed_qty == 0`. `signed_qty = -qty`, `avg_cost = price`.
6. **Add Short**: Additional SELL when `signed_qty < 0`. `new_qty = existing + qty`, `avg_cost = ((abs(existing_qty) * avg_cost) + (qty * price)) / new_qty`.
7. **Reduce Short**: BUY when `qty < abs(existing_signed_qty)`. `signed_qty = existing + qty`. `realized_pnl += qty * (avg_cost - price) - commission`. `avg_cost` remains unchanged.
8. **Close Short**: BUY when `qty == abs(existing_signed_qty)`. `signed_qty = 0`, `status = "CLOSED"`. `realized_pnl += qty * (avg_cost - price) - commission`.
9. **Position Flip (Long to Short)**: SELL with `qty > existing_signed_qty`.
   - Realizes P&L **only** on the closed quantity (`existing_signed_qty`): `existing_qty * (price - avg_cost) - commission`.
   - Opens the remaining quantity (`qty - existing_signed_qty`) as a new Short with `avg_cost = price`.
10. **Position Flip (Short to Long)**: BUY with `qty > abs(existing_signed_qty)`.
   - Realizes P&L **only** on the closed quantity (`abs(existing_signed_qty)`): `abs(existing_signed_qty) * (avg_cost - price) - commission`.
   - Opens the remaining quantity (`qty - abs(existing_signed_qty)`) as a new Long with `avg_cost = price`.
- All financial calculations use exact `Decimal` arithmetic. Binary floating point is strictly prohibited.

### 6.6 Restart Safety & Ambiguous PENDING_SUBMIT Recovery
On startup, `ManualTradingRecoveryService.run_startup_recovery()` scans for non-terminal manual orders:
- Orders left in `PENDING_SUBMIT` without a `broker_order_id` cannot be confirmed as submitted.
- **Absolute Invariant**: The system **NEVER** automatically resubmits an ambiguous order.
- Ambiguous orders are safely marked `ERROR` with reject reason `UNCONFIRMED_BROKER_SUBMISSION_REQUIRES_RECOVERY` and audited.
- If an order has a `broker_order_id`, it remains `SUBMITTED` awaiting normal callback resumption from IBKR.

### 6.7 Source & Engine Isolation
- Manual fills are isolated strictly to `manual_orders`, `manual_executions`, and `manual_positions`.
- Zero engine models (`SignalModel`, `OrderModel`, `PositionModel`, `execution_claims`) are created or mutated.
- Trade Book (`trade_executions`) records manual executions with `order_id = None`, preserving full auditability without engine contamination.

## 7. Milestone M1-D: Reconciliation & Inventory Awareness

Milestone M1-D extends inventory and position reconciliation awareness so that the system correctly accounts for both algorithmic engine positions and manual trading positions when determining expected account inventory.

### 7.1 Core Inventory Equation
For every unique reconciliation key `(account_id, normalized_symbol, normalized_sec_type)`:
```
Expected Net Inventory = Engine Net Quantity + Manual Net Quantity
```
- **Ledger Separation**: `positions` (engine) and `manual_positions` (manual) remain completely independent PostgreSQL tables. They are never merged in the database and are aggregated strictly at the reconciliation boundary.
- **Algebraic Signed Sum**: Longs (+qty) and Shorts (-qty) sum algebraically. If engine has +10 and manual has -10, total expected net is 0.
- **Zero-Net Epsilon Exclusion**: If `abs(total_qty) <= QTY_EPSILON` (1e-5), the net position is omitted from `LedgerNetLine` maps. If the broker is flat (0), no discrepancy is raised. If the broker holds non-zero shares, it is cleanly classified as `BROKER_ORPHAN`.

### 7.2 Breakdown Visibility in Diff Analysis
`LedgerNetLine` and `ReconcileDiff` (as well as API schema `ReconcileDiffRow`) expose:
- `engine_qty`: Net quantity from `positions` (status `OPEN`)
- `manual_qty`: Net quantity from `manual_positions` (status `OPEN`)
- `ledger_qty`: Total aggregated expected net (`engine_qty + manual_qty`)
- `broker_qty`: Actual broker net quantity from IBKR snapshot
- `diff_qty`: `ledger_qty - broker_qty`
- `status`: Diff classification (`MATCH`, `QTY_MISMATCH`, `LEDGER_GHOST`, `BROKER_ORPHAN`)

### 7.3 Rogue Trade & Ghost Protection
- **No Rogue Misclassification**: A legitimate manual position matching broker positions is never marked as `BROKER_ORPHAN` or rogue.
- **No Ghost Misclassification**: Legitimate engine positions matching broker positions are never marked as `LEDGER_GHOST` when manual positions exist on other symbols or accounts.
- **Exact Offsets**: When engine and manual hold opposing positions resulting in zero net, if broker is also 0, diff classification is omitted (no false ghost).

### 7.4 In-Flight Order Awareness
`fetch_in_flight_accounts()` queries both:
1. Engine orders with status `PENDING`, `SUBMITTED`, `PARTIALLY_FILLED`
2. Manual orders with status `PENDING_SUBMIT`, `SUBMITTED`, `PARTIALLY_FILLED`

Accounts with active manual orders in flight are marked as `in_flight = True`, preventing reconciliation sync loops or premature diff flagging while manual executions are settling.

### 7.5 Broker Align Safety Invariant
In `BrokerAlignService._do_align_line()`, target quantity calculation calls:
```python
ledger_net_qty_for_symbol(session, account_id, sym, sec_type, manual_open_rows=manual_open_rows)
```
Target quantity is therefore strictly `engine_qty + manual_qty`. This ensures that broker align operations align to total portfolio intent and **never** inadvertently flatten or reverse legitimate manual positions.

### 7.6 Multi-Account Isolation
All queries strictly filter by `account_id`. Manual positions in Account A do not affect reconciliation lines, expected inventory, or diffs for Account B.

---

## 8. Milestone M1-E: Manual Order Cancellation, Callback Hardening & Invariants

Milestone M1-E delivers production-grade manual order cancellation, callback state-machine hardening, idempotent cancel semantics, terminal lifecycle invariants, and persistent UI management with strict account isolation.

### 8.1 Cancellation API Contract
`POST /api/v1/manual/orders/{order_id}/cancel?ibkr_account={ibkr_account}`

- **Account Authorization & Identity Resolution**:
  - The request resolves the manual order server-side via `order_id` strictly scoped to the caller's authorized `account_id` / `ibkr_account`.
  - Cross-account attempts or engine orders return `404 Not Found` (never leaking order existence).
  - The frontend never supplies an arbitrary `broker_order_id`.

- **Cancellable vs Terminal States**:
  - **Cancellable States**: `PENDING_SUBMIT`, `SUBMITTED`, `PARTIALLY_FILLED`.
  - **Terminal States**: `FILLED`, `CANCELLED`, `REJECTED`, `ERROR`.
  - **Already Cancelled**: Idempotently returns `200 OK` with `status = "CANCELLED"` and `is_cancelled = True` without invoking the broker.
  - **Non-Cancellable Terminal States (`FILLED`, `REJECTED`, `ERROR`)**: Rejects with `400 Bad Request`. Zero broker calls; zero position or P&L mutation.

- **Local vs Broker Cancellation**:
  - If `PENDING_SUBMIT` and `broker_order_id IS NULL`: The order was never placed with IBKR. It is transitioned locally to `CANCELLED`, audited with `MANUAL_ORDER_CANCELLED`, and zero broker calls or rate-limiter tokens are consumed.
  - If `SUBMITTED` or `PARTIALLY_FILLED`:
    1. Acquires `GatewayRateLimiter` token with `priority = PRIORITY_ORDER_EXECUTION` (1) and action label `"cancelOrder"`.
    2. Calls `client.cancelOrder(int(broker_order_id))`.
    3. Records audit event `MANUAL_ORDER_CANCEL_REQUESTED`.
    4. Transition to terminal `CANCELLED` is finalized upon receipt of IBKR's `orderStatus(status="Cancelled")` callback.

- **Response Body**:
```json
{
  "order_id": 42,
  "internal_order_id": "MAN_9F2B8D3A01E2",
  "broker_order_id": 5001,
  "perm_id": 123456789,
  "status": "CANCELLED",
  "is_cancelled": true,
  "message": "Manual order cancellation requested with IBKR"
}
```

### 8.2 Broker Mutation Safety Constraints
The cancellation pathway enforces strict broker safety invariants:
- **Only Allowed Broker Mutation**: `client.cancelOrder(int(broker_order_id))` for the exact verified `broker_order_id`.
- **Strictly Forbidden Operations**:
  - Zero `reqGlobalCancel`
  - Zero `placeOrder`
  - Zero `emergency_flatten`
  - Zero `square_off`
  - Zero `flatten`
  - Zero secondary broker clients or sockets

### 8.3 Callback State Machine & Lifecycle Invariants
1. **Permanently Terminal `FILLED`**:
   - `FILLED` is immutable.
   - Transitions `FILLED -> CANCELLED`, `FILLED -> PARTIALLY_FILLED`, `FILLED -> SUBMITTED`, `FILLED -> REJECTED`, or `FILLED -> ERROR` are strictly forbidden.
   - If a late `Cancelled` or `Submitted` callback arrives after an order is already `FILLED`, the callback updates non-lifecycle metadata (such as newly learned `perm_id`) but **never** overwrites the `FILLED` status.

2. **Reconnect & `openOrder` Resurrect Protection**:
   - Reconnect callbacks (`openOrder`) during gateway reconnect replay open orders.
   - Replay callbacks must **never** resurrect terminal orders (`CANCELLED -> SUBMITTED`, `FILLED -> SUBMITTED`, `REJECTED -> SUBMITTED`, `ERROR -> SUBMITTED`).
   - `PARTIALLY_FILLED` orders cannot regress to `SUBMITTED`.

3. **Out-of-Order Execution Details & Accounting Truth**:
   - Execution callbacks (`execDetails`) reflect real broker executions.
   - Executions are permanently recorded in `manual_executions` and applied to `manual_positions` with exact `Decimal` math, even if the order was previously marked `CANCELLED` (e.g., late partial fill reported after cancellation request).
   - Late execution on a `CANCELLED` order updates `total_filled`:
     - If `total_filled >= order.quantity`: The order is marked `FILLED`.
     - If `total_filled < order.quantity`: The order lifecycle remains `CANCELLED` (accounting reflects the partial fill, and the remaining balance is cancelled).
   - Cancellation itself produces **zero executions**, **zero position rollbacks**, and **zero artificial P&L mutations**.

### 8.4 Concurrency & Race Condition Defenses
- **Double-Click & Concurrent API Requests**: Row-level locking (`SELECT ... FOR UPDATE` via `repo.get_by_id(..., for_update=True)`) guarantees atomic transition checks. If a second cancel request arrives concurrently, the first request transitions or dispatches, and the second observes the in-flight or cancelled state idempotently.
- **Fill vs. Cancel Race**: If a fill occurs while a cancel is in-flight, the execution callback is ingested first or second. In both orderings, executions are preserved, positions are updated, and the terminal state reflects either `FILLED` (full quantity) or `CANCELLED` with partial executions recorded.

### 8.5 Frontend Persistence & Interactive Cancellation
- **Persistent Order Loading**: `ManualTradePage` polls `GET /api/v1/manual/orders` every 5 seconds, displaying durable manual orders surviving browser refreshes and backend restarts.
- **Interactive Cancel**:
  - Cancel button is conditionally rendered only for cancellable orders (`PENDING_SUBMIT`, `SUBMITTED`, `PARTIALLY_FILLED`).
  - Terminal orders (`FILLED`, `CANCELLED`, `REJECTED`, `ERROR`) display status badges with no cancel action.
  - Double-click protection disables buttons and displays a loading spinner during cancellation in-flight.
  - Refetches orders and positions immediately upon cancellation response.

