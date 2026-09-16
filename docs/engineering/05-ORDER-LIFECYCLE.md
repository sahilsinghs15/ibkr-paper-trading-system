# 05-ORDER-LIFECYCLE.md — Order Lifecycles & State Machines

---

## 1. ENGINE (ALGORITHMIC) ORDER LIFECYCLE

```
[TradingView Webhook] 
       │ POST :8000/api/webhooks/tradingview
       ▼
[Webhook Ingest] ────► Computes Idempotency Key ────► Inserts `signal_jobs` (QUEUED)
                                                                │
┌───────────────────────────────────────────────────────────────┘
▼
[WorkerPool (10 Workers)]
       │ Claims Job (lease_expires_at = now() + 30s)
       ▼
[OrderManager.process_signal_execution]
       │
       ├─► 1. Global Red Zone Check (SessionClock.projected_in_red_zone)
       │      └─► If true: parks job as `DEFERRED_RED_ZONE`
       │
       ├─► 2. Resolve Accounts (DatabaseStrategyAccountRouter)
       │
       ├─► 3. Size Intent (ModelBlueStrategy / Sizer)
       │
       ├─► 4. Hard Pre-Submit Checks:
       │      ├─► is_account_kill_switch_active(account_id)
       │      └─► is_account_trading_paused(account_id)
       │
       ├─► 5. Exposure Guard: Acquires async locks for (account, symbol) & margin
       │
       ├─► 6. RMS Engine: Sequential evaluation of checks 1, 2, 3, 4, 7, 8, 101
       │
       ├─► 7. Instrument Resolution: Resolves STK/CFD and con_id
       │
       ├─► 8. Execution Claim Barrier:
       │      └─► ExecutionClaimRepository.acquire(dedupe_key) (COMMITTED TO DB)
       │
       ▼
[BasketCoordinator.execute]
       │
       ├─► Paces outbound message: GatewayRateLimiter.acquire(PRIORITY_ORDER_EXECUTION)
       ├─► Allocates order ID: TWSClient.allocate_next_order_id()
       ├─► Submits to IBKR: TWSClient.placeOrder()
       ├─► Inserts order row in `orders` table (status='SUBMITTED')
       │
       ▼
[Awaiting Broker Callbacks]
       │
       ├─► All legs filled within fill_timeout (90s)
       │      ├─► Basket state -> OPEN / CLOSED
       │      ├─► ExecutionClaimRepository.mark_executed(dedupe_key)
       │      ├─► OrderManager updates runtime position in `positions` table
       │      └─► LivePnlService.watch_open(intent)
       │
       └─► Partial fill / timeout / rejection
              ├─► Basket state -> COMPENSATING
              ├─► Emits unwind orders for filled legs
              ├─► If unwound successfully: Basket state -> COMPENSATED
              └─► If unwind fails: Basket state -> BASKET_CRITICAL (blocks strategy)
```

---

## 2. MANUAL CFD ORDER LIFECYCLE

```
[Trader in UI]
       │
       ├─► 1. Click "Preview Order" -> POST :8001/api/v1/manual/orders/preview
       │      ├─► ManualTradingService.validate_pretrade:
       │      │     - Gateway connected
       │      │     - Account enabled
       │      │     - Manual halt clear (`manual_halt_state`)
       │      │     - Kill switch inactive
       │      │     - Trading pause checked (reducing allowed, new opens blocked)
       │      │     - Contract CFD validated (sec_type='CFD', con_id > 0)
       │      │     - Order type: LIMIT or MARKET only (STOP forbidden)
       │      │     - Quantity > 0, Price > 0, minTick aligned
       │      └─► What-If Margin Probe: IBKRExecutionAdapter.probe_margin()
       │            └─► Returns notional and initial/maintenance margin changes (Non-mutating)
       │
       ▼
[Trader Confirms Submit]
       │ POST :8001/api/v1/manual/orders/submit
       │
       ├─► 2. Server re-validates all pre-trade safety gates
       │
       ├─► 3. Idempotency Check (ManualOrderRepository.get_by_idempotency_key):
       │      ├─► Exact match -> Returns existing order (Idempotent replay, no duplicate placed)
       │      └─► Parameter conflict -> Raises HTTP 409 Conflict
       │
       ├─► 4. Phase 1 Commit:
       │      ├─► Insert row into `manual_orders` with status='PENDING_SUBMIT'
       │      └─► Explicitly COMMIT transaction before broker call!
       │
       ├─► 5. Broker Call:
       │      ├─► GatewayRateLimiter.acquire(PRIORITY_ORDER_EXECUTION, 'placeOrder')
       │      ├─► Broker order ID allocated: TWSClient.allocate_next_order_id()
       │      ├─► Build Contract (secType='CFD', exchange='SMART', currency='USD')
       │      ├─► Build Order (outsideRth=False, eTradeOnly=False, firmQuoteOnly=False, orderRef=MAN_...)
       │      └─► Transmit: TWSClient.placeOrder(broker_order_id, contract, ib_order)
       │
       ├─► 6. Phase 2 Commit:
       │      ├─► Update `manual_orders` status='SUBMITTED', broker_order_id, submitted_at
       │      ├─► Record audit event in `manual_audit_events` ('MANUAL_ORDER_SUBMITTED')
       │      └─► Commit transaction
       │
       ▼
[Broker Asynchronous Callbacks]
       │
       ├─► orderStatus() -> Updates status to PARTIALLY_FILLED or FILLED
       └─► execDetails() -> Dispatches to ManualExecutionListener (mutates position)
```

---

## 3. ORDER STATE MACHINE INVARIANTS

1. **Terminal State Irreversibility**:
   - Once an order reaches `FILLED`, `CANCELLED`, `REJECTED`, or `ERROR`, subsequent broker callbacks **CANNOT** revert it to an active state.
2. **Cancellation with Partial Fills**:
   - If an order is cancelled at the broker but receives late execution fills, it retains status `CANCELLED` while recording the executed shares.
3. **SUBMITTED $\neq$ FILLED**:
   - Marking an order `SUBMITTED` only acknowledges broker transmission. It does not grant position ownership until `execDetails` confirms the fill.
