- **MARGIN** — sufficient margin hai ya nahi
- **DUPLICATE** — duplicate order/trade toh nahi
- **STRATEGY** — strategy validation
- **CONTRACT_MONTH** — correct contract month
- **OPEN_POSITION_LIMIT** — open-position limit check
- **MONEY_PER_STOCK** — per-stock money limit
- **MODEL_MARKET_VALUE** — model market-value limit

Basket Coordinator

OrderIntent
   ↓
BasketCoordinator.execute()
   ↓
Basket create → PENDING
   ↓
Basket → EXECUTING
   ↓
OMSService.submit_intent()
   ↓
_submit_leg()
   ↓
IBKRExecutionAdapter.submit_order()
   ↓
IBKR
   ↓
Fills / callbacks
   ↓
Basket state update
   ↓
COMPLETE

### Account routing + Strategy + Sizing

```
Signal
  ↓
DatabaseStrategyAccountRouter.resolve()
  ↓
Eligible Accounts
  ↓
OrderManager._fanout_accounts()
  ↓
_fanout_single_account()
  ↓
ModelBlueStrategy.build_intent()
  ↓
ModelBlueSizer.size_open()
  ↓
OrderIntent
```

### Webhook → Worker Pool

```
TradingView
   ↓
POST /api/webhooks/tradingview
   ↓
receive_tradingview_webhook()
   ↓
Authentication
   ↓
_process_tradingview_webhook()
   ↓
PostgreSQL → signal_jobs
   ↓
ExecutionWorkerPool
   ↓
_claim_job()
   ↓
_execute_job()
```

![[system_architecture.png]]