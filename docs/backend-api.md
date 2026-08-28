# Backend HTTP API

**Verified from:** `backend/app/main.py`, `backend/app/api/router.py`, `backend/app/api/routes/health.py`, `backend/app/api/routes/webhooks.py`, `backend/app/api/routes/orders.py`, `backend/app/api/routes/config.py`, `backend/app/schemas/api_schemas.py`, `backend/app/schemas/config_schemas.py`, `backend/app/schemas/webhook.py`, `backend/demo_streaming/api.py`.

## Main app (`app.main:app`)

Mounted in `create_app()`:

- `health_router` — no prefix
- `webhooks_router` — prefix `/api`
- `api_router` — prefix `/api/v1` (orders + baskets + config + system-monitor + reconcile routers)

**No** `CORSMiddleware`, **no** WebSocket routes, **no** `StaticFiles` / HTML mount on this app.

### Endpoints (complete list)

| Method | Path | Handler | Request | Response | Side effects |
|--------|------|---------|---------|----------|--------------|
| `GET` | `/health` | `get_health` | — | `{"status":"ok"}` | None |
| `POST` | `/api/webhooks/tradingview` | `receive_tradingview_webhook` | Raw JSON object body | `TradingViewWebhookResponse`: `status`, `source`, `signal_id`, `job_id`, `request_id` | Disk capture; parse; enqueue `signal_jobs` when `session_factory` present |
| `GET` | `/api/v1/orders` | `get_orders` | — | `list[OrderSchema]` | Reads in-memory OMS orders |
| `GET` | `/api/v1/orders/{order_id}` | `get_order_by_id` | path id | `OrderSchema` or 404 | In-memory OMS lookup |
| `DELETE` | `/api/v1/orders/{order_id}` | `cancel_order` | path id | `OrderSchema` or 404/400 | `OMSService.cancel_order` → broker cancel |
| `GET` | `/api/v1/config/accounts` | `list_accounts_config` | — | `AccountsConfigResponse` | Read Postgres accounts, allocations, symbol limits |
| `GET` | `/api/v1/config/accounts/by-identifier/{ibkr_account}` | `get_account_by_identifier` | path | `AccountConfigSchema` | Lookup by IBKR account string |
| `POST` | `/api/v1/config/accounts` | `create_account` | `CreateAccountRequest` | `AccountConfigSchema` (201) | Create account row |
| `PATCH` | `/api/v1/config/accounts/{account_id}` | `patch_account` | `PatchAccountRequest` | `AccountConfigSchema` | Update name / ibkr_account / margin / enabled |
| `GET` | `/api/v1/config/accounts/{account_id}/deletable` | `check_account_deletable_api` | — | `AccountDeleteCheckResponse` | Pre-delete safety check |
| `DELETE` | `/api/v1/config/accounts/{account_id}` | `delete_account_api` | — | 204 | Delete account (no trading history) |
| `POST` | `/api/v1/config/accounts/{account_id}/square-off` | `square_off_account_positions` | — | `SquareOffResponse` (202) | Kill switch: emergency flatten |
| `GET` | `/api/v1/config/accounts/{account_id}/kill-switch` | `get_account_kill_switch_status` | — | `KillSwitchStatusResponse` | Armed? |
| `POST` | `/api/v1/config/accounts/{account_id}/kill-switch/clear` | `clear_account_kill_switch_endpoint` | — | `KillSwitchClearResponse` | Disarm kill switch |
| `POST` | `/api/v1/emergency-kill-switch` | `emergency_kill_switch_endpoint` | `EmergencyKillSwitchRequest` | `EmergencyKillSwitchResponse` | Pre-flight webhook: arm existing Kill Switch (NO broker flatten on EC2) |
| `POST` | `/api/v1/config/accounts/{account_id}/allocations` | `create_account_allocation` | `CreateAllocationRequest` | `AllocationConfigSchema` (201) | Create allocation |
| `PATCH` | `/api/v1/config/allocations/{allocation_id}` | `patch_allocation` | `PatchAllocationRequest` | `AllocationConfigSchema` | Update alloc_pct / enabled / max_open_positions |
| `PUT` | `/api/v1/config/accounts/{account_id}/symbol-limits/{symbol}` | `put_symbol_limit` | `PutSymbolLimitRequest` | `SymbolLimitSchema` | Upsert limit; reload RMS limits |
| `DELETE` | `/api/v1/config/accounts/{account_id}/symbol-limits/{symbol}` | `delete_symbol_limit` | — | 204 | Delete limit; reload RMS limits |
| `GET` | `/api/v1/config/execution` | `get_execution_settings` | — | `ExecutionSettingsSchema` | Read/create singleton paper retry row |
| `PATCH` | `/api/v1/config/execution` | `patch_execution_settings` | `PatchExecutionSettingsRequest` | `ExecutionSettingsSchema` | Persist retry knobs; reload basket coordinator |
| `GET` | `/api/v1/system-monitor` | `get_system_monitor` | — | `SystemMonitorResponse` | Read-only EC2/service observability |
| `GET` | `/api/v1/reconcile/positions` | `get_reconcile_positions` | query `ibkr_account` (optional) | `ReconcilePositionsResponse` | Latest `broker_positions` snapshot, OPEN ledger rows, fresh diffs (no live `reqPositions`) |
| `POST` | `/api/v1/reconcile/positions/flatten` | `flatten_broker_position_line` | `FlattenBrokerPositionRequest` | `FlattenBrokerPositionResponse` | MARKET flatten one broker snapshot line (qty from DB); no kill switch, no ledger close |
| `GET` | `/api/v1/baskets/critical` | `list_critical_baskets` | query `ibkr_account` (required) | `CriticalBasketsResponse` | CRITICAL baskets with recovery status and leg fill summary; empty list = OPEN latch cleared |

`OrderSchema` fields: `order_id`, `symbol`, `side`, `quantity`, `order_type`, `status`, `timestamp`, `price`, `filled_quantity`, `average_fill_price`.

Config validation errors → HTTP 400 with `AllocationConfigError` message in `detail`.

Account create/patch fields: `name`, `ibkr_account`, `total_margin`, `enabled` (`schemas/config_schemas.py`). **No** gateway host, port, clientId, or binding. `ibkr_account` is the IB account string copied onto `IBOrder.account`, not a socket selector.

Webhook HTTP **202 Accepted**. `status` values: `accepted`, `rejected` (invalid payload). Legacy inline path may return `rejected_by_rms`. Invalid JSON → HTTP 400.

Global unhandled `Exception` → HTTP 500 `{"detail":"Internal server error. Please try again later."}`.

### Schemas

`app/schemas/api_schemas.py` defines only `OrderSchema` (used by `api/routes/orders.py`). Config and webhook schemas live in `config_schemas.py` and `webhook.py`. Earlier unrouted schemas (`SignalSchema`, `PositionSchema`, `MarginSchema`, `PlaceOrderRequest`, `ModifyOrderRequest`, `BrokerStatusResponse`) were removed — `POSTMAN_API_TESTING_GUIDE.md` still references them and is stale.

## Demo stream app (`demo_streaming`, separate process)

Default bind: `127.0.0.1:8010` (`demo_streaming/config.py`). Does **not** connect to IBKR. Read-only for positions; **proxies** config writes to the trading app.

| Method | Path | Role |
|--------|------|------|
| `GET` | `/health` | Redis ping; `{status, redis, stream, mode:"read-only"}` |
| `GET` | `/demo/positions` | Snapshot of OPEN positions from Postgres |
| `GET` | `/demo/closed-positions` | Closed positions (optional `account_id`) |
| `GET` | `/demo/signals` | Signal/job history with pagination and filters |
| `GET` | `/demo/market-data-health` | Live PnL subscription health (if service attached) |
| `GET` | `/demo/stream` | SSE from Redis stream |
| `GET/POST/PATCH/PUT/DELETE` | `/api/v1/config/*` | Proxy to trading app (`TRADING_API_URL`, default `http://127.0.0.1:8000`) |
| `GET` | `/` | React build `frontend/dist/index.html` if present; else static fallback |
| `GET` | `/settings` | SPA fallback (same as `/`) |
| `GET` | `/accounts` | SPA fallback |
| `GET` | `/account/{path}` | SPA fallback |
| `GET` | `/assets/*` | Mounted when `frontend/dist/assets` exists (Vite build) |

Set `DEMO_STREAM_HOST=0.0.0.0` to listen on all interfaces. Do not confuse with ngrok on `:8000`.

## Historical Postman guide

[`../backend/POSTMAN_API_TESTING_GUIDE.md`](../backend/POSTMAN_API_TESTING_GUIDE.md) is **not** an accurate inventory. Do not implement or document endpoints from it unless they appear in the tables above.

## Related docs

- Kill switch endpoints: [`backend-kill-switch.md`](backend-kill-switch.md)
- Execution settings: [`backend-rms-oms.md`](backend-rms-oms.md)
- Gateway binding APIs: **not implemented** — [`backend-multi-gateway.md`](backend-multi-gateway.md)
