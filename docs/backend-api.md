# Backend HTTP API

**Verified from:** `backend/app/main.py`, `backend/app/api/router.py`, `backend/app/api/routes/health.py`, `backend/app/api/routes/webhooks.py`, `backend/app/api/routes/orders.py`, `backend/app/api/routes/config.py`, `backend/app/schemas/api_schemas.py`, `backend/app/schemas/config_schemas.py`, `backend/app/schemas/webhook.py`, `backend/demo_streaming/api.py`.

## Main app (`app.main:app`)

Mounted in `create_app()`:

- `health_router` — no prefix
- `webhooks_router` — prefix `/api`
- `api_router` — prefix `/api/v1` (orders + config routers)

**No** `CORSMiddleware`, **no** WebSocket routes, **no** `StaticFiles` / HTML mount on this app.

### Endpoints (complete list)

| Method | Path | Handler | Request | Response | Side effects |
|--------|------|---------|---------|----------|--------------|
| `GET` | `/health` | `get_health` | — | `{"status":"ok"}` | None |
| `POST` | `/api/webhooks/tradingview` | `receive_tradingview_webhook` | Raw JSON object body | `TradingViewWebhookResponse`: `status`, `source` (`tradingview`) | Write capture under `backend/data/tradingview_webhooks/`; parse; persist signal; run `OrderManager.process_signal_execution` when `app.state.order_manager` is set |
| `GET` | `/api/v1/orders` | `get_orders` | — | `list[OrderSchema]` | Reads in-memory OMS orders |
| `GET` | `/api/v1/orders/{order_id}` | `get_order_by_id` | path id | `OrderSchema` or 404 | In-memory OMS lookup |
| `DELETE` | `/api/v1/orders/{order_id}` | `cancel_order` | path id | `OrderSchema` or 404/400 | `OMSService.cancel_order` → broker cancel |
| `GET` | `/api/v1/config/accounts` | `list_accounts_config` | — | `AccountsConfigResponse` | Read Postgres accounts, allocations, symbol limits |
| `PATCH` | `/api/v1/config/accounts/{account_id}` | `patch_account` | `PatchAccountRequest` | `AccountConfigSchema` | Update `total_margin` / `enabled` |
| `PATCH` | `/api/v1/config/allocations/{allocation_id}` | `patch_allocation` | `PatchAllocationRequest` | `AllocationConfigSchema` | Update `alloc_pct`, `enabled`, `max_open_positions` |
| `PUT` | `/api/v1/config/accounts/{account_id}/symbol-limits/{symbol}` | `put_symbol_limit` | `PutSymbolLimitRequest` | `SymbolLimitSchema` | Upsert `per_symbol_limits`; reload in-memory RMS limits |
| `DELETE` | `/api/v1/config/accounts/{account_id}/symbol-limits/{symbol}` | `delete_symbol_limit` | — | 204 | Delete limit row; reload in-memory RMS limits |
| `GET` | `/api/v1/config/execution` | `get_execution_settings` | — | `ExecutionSettingsSchema` | Read/create singleton paper square-off/retry row |
| `PATCH` | `/api/v1/config/execution` | `patch_execution_settings` | `PatchExecutionSettingsRequest` | `ExecutionSettingsSchema` | Persist retry knobs; reload basket coordinator |

`OrderSchema` fields: `order_id`, `symbol`, `side`, `quantity`, `order_type`, `status`, `timestamp`, `price`, `filled_quantity`, `average_fill_price`.

Config validation errors → HTTP 400 with `AllocationConfigError` message in `detail`.

Webhook `status` values used in code: `received`, `rejected`, `rejected_by_rms`, `execution_incomplete`. Invalid JSON → HTTP 400.

Global unhandled `Exception` → HTTP 500 `{"detail":"Internal server error. Please try again later."}`.

### Schemas defined but unused by any router

In `app/schemas/api_schemas.py` (no matching route modules):

- `SignalSchema`
- `PositionSchema`
- `MarginSchema`
- `PlaceOrderRequest`
- `ModifyOrderRequest`
- `BrokerStatusResponse`

## Demo stream app (`demo_streaming`, separate process)

Default bind: `127.0.0.1:8010` (`demo_streaming/config.py`). Does **not** connect to IBKR. Read-only for positions; **proxies** config writes to the trading app.

| Method | Path | Role |
|--------|------|------|
| `GET` | `/health` | Redis ping; `{status, redis, stream, mode:"read-only"}` |
| `GET` | `/demo/positions` | Snapshot of OPEN positions from Postgres |
| `GET` | `/demo/stream` | SSE from Redis stream |
| `GET/PUT/PATCH/DELETE` | `/api/v1/config/*` | Proxy to trading app (`TRADING_API_URL`, default `http://127.0.0.1:8000`) |
| `GET` | `/` | React build `frontend/dist/index.html` if present; else `demo_streaming/static/index.html` |
| `GET` | `/settings` | SPA fallback (same as `/`) |
| `GET` | `/assets/*` | Mounted when `frontend/dist/assets` exists (Vite build) |

Default bind: `127.0.0.1:8010`. Set `DEMO_STREAM_HOST=0.0.0.0` to listen on all interfaces (e.g. server public IP). Do not confuse with ngrok on `:8000`.

## Historical Postman guide

[`../backend/POSTMAN_API_TESTING_GUIDE.md`](../backend/POSTMAN_API_TESTING_GUIDE.md) is **not** an accurate inventory. Do not implement or document endpoints from it unless they appear in the tables above.
