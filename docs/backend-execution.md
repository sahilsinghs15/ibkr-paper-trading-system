# Backend execution path (debug orders)

**Verified from:** `backend/app/api/routes/webhooks.py`, `backend/app/services/order_manager.py`, `backend/app/services/strategies/inbound.py`, `backend/app/accounts/router.py`, `backend/app/services/model_blue/`, `backend/app/rms/engine.py`, `backend/app/oms/coordinator.py`, `backend/app/oms/ibkr_adapter.py`, `backend/app/main.py`, `backend/app/core/logger.py`.

Use this file when a signal is missing, rejected, or incompletely filled. Root [`AGENTS.md`](../../AGENTS.md) points here as `app/docs/backend-execution.md`.

## Process shape

One FastAPI process (`app.main:app`). Lifespan wires:

`TWSClient` → `IBKRExecutionAdapter` → `OMSService` → `OrderManager` (+ `ModelBlueExecutionPersistence`, `DatabaseCommittedCapitalProvider`, `DatabaseModelBlueTradeBook`, `LivePnlService`) onto `app.state`.

There is **no** separate Listener / Strategy / per-account OMS / Risk process in this codebase.

## Live path (OPEN / CLOSE)

```
POST /api/webhooks/tradingview
  → receive_tradingview_webhook
  → write capture JSON under backend/data/tradingview_webhooks/
  → OrderManager.parse_inbound_payload
       → strategies/inbound.parse_tradingview_payload
       → ModelBlueStrategy when strategy_id == "model_blue" (else legacy parse)
  → OrderManager.process_signal_execution
       → persist SignalModel (status NEW) when possible
       → DatabaseStrategyAccountRouter.resolve(strategy_id)
            (enabled Account × Strategy × Allocation rows only)
       → per AccountExecutionContext: ModelBlueStrategy.build_intent (sizer + trade book)
       → RMSEngine.evaluate (checks 2, 3, 4, 7, 8)
       → instrument resolve (catalog / paper STK→CFD override; auto CFD conId discovery when missing)
       → BasketCoordinator.execute (preferred when OMSService present)
            → OMSService / IBKRExecutionAdapter.placeOrder
            → TWSClient → IBKR
       → ModelBlueExecutionPersistence + position / trade-book updates
       → LivePnlService.watch_open / unwatch
```

Webhook HTTP outcomes (from `webhooks.py`):

| `status` field | Meaning in code |
|----------------|-----------------|
| `received` | Default success path, or broker connection error swallowed as received |
| `rejected` | Invalid payload (`ValueError` at parse) |
| `rejected_by_rms` | RMS / signal rejection (`all_rejected` or `ValueError` from pipeline) |
| `execution_incomplete` | Pipeline returned `success=False` with some work attempted |

Malformed JSON → HTTP 400. Unhandled pipeline exception → HTTP 500.

## What runs at startup

From `main.py` lifespan:

1. `setup_logging(level=settings.log_level)` → daily file `storage/logs/trading-YYYY-MM-DD.log` + stderr.
2. Build adapter / OMS / OrderManager.
3. `order_manager.hydrate_runtime_from_db()` (RMS / Model Blue runtime from Postgres).
4. `client.connect_and_start(...)` to `ibkr_host:ibkr_port`.
5. On successful connect: `hydrate_live_pnl()`.
6. Store `client`, `ibkr_adapter`, `oms`, `order_manager` on `app.state`.

Orders listed by `GET /api/v1/orders` come from the **in-memory** OMS map, not a SQL query.

## Log file and useful greps

Logger: `backend/app/core/logger.py` → `/home/tradingapp/storage/logs/trading-YYYY-MM-DD.log` (midnight rollover; one file per day).

Format: `%(asctime)s | %(levelname)-8s | %(name)s | %(trace)s | %(message)s` where `trace` carries `req=` / `signal=` / `trade=` / `acct=` from ContextVars (or `-`).

Useful substrings present in code:

| Grep | Stage |
|------|--------|
| `Received TradingView webhook payload safely` | Webhook accepted + captured |
| `Webhook HTTP response status=` | Final HTTP status returned to TradingView |
| `Rejected invalid TradingView payload` | Parse failure |
| `Inbound parse:` / `Model Blue parse` | Strategy handler / payload parse |
| `Account router resolved` | Eligible accounts |
| `Model Blue size_open` / `OPEN intent` / `CLOSE intent` | Sizing |
| `RMS check` / `RMS evaluate` | Per-check RMS trail |
| `BASKET_CRITICAL gate blocked` | CRITICAL open block |
| `Basket fill wait complete` | Per-leg completeness after fill wait |
| `Signal processed by OrderManager -> RMS -> OMS` | Pipeline finished for a signal |
| `Incoming TradingView signal rejected` | Pipeline `ValueError` |
| `Signal ingested but broker submission unconfirmed` | ConnectionError / RuntimeError after ingest |
| `Error processing signal through OrderManager pipeline` | Unhandled exception |
| `Active execution pipeline: IBKRExecutionAdapter` | Lifespan ready |
| `Initial TWS connection attempt unconfirmed` | Connect failed at startup |
| `BASKET_CREATED` / `BASKET_UNWINDING` / `BASKET_COMPENSATED` / `BASKET_CRITICAL` | Basket coordinator (app log + event_log) |
| `POSITION_OPEN persisted` / `POSITION_CLOSE persisted` | Model Blue persistence |
| `CFD discover upserted` / `CFD discover:` | Auto CFD conId discovery (`instruments` master) |
| `LivePnl reqMktData` / `LivePnl reqMktData without conId` | PnL market-data subscribe (CFD + conId) |
| `TWS placeOrder failed` | Adapter place failure |

## Not on this path

- No automatic target / stop / time_limit exit loop (columns may exist on positions; no risk-engine trigger process found).
- No dashboard kill-switch HTTP API.
- Demo SSE UI (`demo_streaming`) is a **separate** process; it does not place orders.
