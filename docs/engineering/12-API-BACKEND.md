# 12-API-BACKEND.md — API Architecture & Dependencies

---

## 1. DUAL APPLICATION SURFACE

The system exposes two distinct FastAPI application instances:

### Ingest Application (`backend/app/webhook_ingest.py`, `:8000`)
- **Purpose**: Webhook intake only.
- **Routers**:
  - `/health`: Health status.
  - `/api/webhooks/tradingview`: Ingests TradingView alert JSON. Returns HTTP 202 with `job_id`.

### Execution Application (`backend/app/main.py`, `:8001`)
- **Purpose**: Order execution, manual trading, configuration, monitoring.
- **Prefix**: `/api/v1`
- **Routers**:
  - `/auth`: User login, token refresh, `/me` profile.
  - `/orders`: Engine orders ledger query and cancellation.
  - `/manual`: Manual CFD order preview, submission, position query, close, and halt.
  - `/baskets`: Basket status and `BASKET_CRITICAL` queries.
  - `/config`: Account settings, allocations, strategy configurations, trading pause, kill switch.
  - `/reconcile`: Trigger reconcile sweeps, query status, manual position align.
  - `/system-monitor`: System metrics and AWS Cost Explorer credit ledger.
  - `/broker`: Trade Book execution sync queries and manual execution triggers.

---

## 2. AUTHENTICATION & AUTHORIZATION (`app/api/deps.py`)

- **JWT Tokens**: Bearer tokens verified via `decode_access_token()`.
- **Role Isolation**:
  - `admin`: Full access to all accounts, configuration, kill switch, system monitor, and audit logs.
  - `user`: Restricted to their assigned `ibkr_account_id`.
- **Testing Override**: If `TRADINGAPP_TESTING=1` under pytest, a synthetic test admin user is injected if no token is supplied.

---

## 3. DEPENDENCY INJECTION RULES

- All route dependencies must be declared via FastAPI `Depends()` in `deps.py`.
- Global components (`oms`, `order_manager`, `session_factory`) are retrieved from `request.app.state`.
- Routes **MUST NOT instantiate services with unmanaged connections**; always pass the request-scoped `session` or application-level singleton.
