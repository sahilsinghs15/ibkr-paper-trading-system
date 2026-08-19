# Backend configuration

**Verified from:** `backend/app/core/config.py`, `backend/demo_streaming/config.py`.

Settings load from environment variables and optional `.env` (`SettingsConfigDict`, `extra="ignore"`). Unknown keys (including historical `BROKER_MODE`, `ALLOCATIONS_CONFIG_PATH`) are **ignored** and are **not** fields on `Settings`.

Use `get_settings()`; do not construct `Settings()` ad hoc in new code.

## `Settings` fields (`app.core.config`)

| Field | Env (uppercase) | Default | Notes |
|-------|-----------------|---------|-------|
| `app_name` | `APP_NAME` | `"IBKR Paper Trading System"` | FastAPI title |
| `environment` | `ENVIRONMENT` | `"development"` | |
| `log_level` | `LOG_LEVEL` | `"INFO"` | Passed to `setup_logging` |
| `database_url` | `DATABASE_URL` | `postgresql+asyncpg://root:root123@localhost:5433/ibkr_trading` | Async SQLAlchemy URL |
| `ibkr_host` | `IBKR_HOST` | `"127.0.0.1"` | |
| `ibkr_port` | `IBKR_PORT` | `7497` | Paper TWS default; **not** validated against live ports |
| `ibkr_client_id` | `IBKR_CLIENT_ID` | `1` | |
| `ibkr_connection_timeout` | `IBKR_CONNECTION_TIMEOUT` | `10` | Seconds |
| `ibkr_market_data_type` | `IBKR_MARKET_DATA_TYPE` | `3` | IBKR market data type (1–4) |
| `ibkr_market_data_symbol` | `IBKR_MARKET_DATA_SYMBOL` | `"AAPL"` | |
| `ibkr_market_data_sec_type` | `IBKR_MARKET_DATA_SEC_TYPE` | `"STK"` | |
| `ibkr_market_data_exchange` | `IBKR_MARKET_DATA_EXCHANGE` | `"SMART"` | |
| `ibkr_market_data_currency` | `IBKR_MARKET_DATA_CURRENCY` | `"USD"` | |
| `ibkr_market_data_primary_exchange` | `IBKR_MARKET_DATA_PRIMARY_EXCHANGE` | `None` | |
| `trading_symbol` | `TRADING_SYMBOL` | `"RELIANCE"` | Passed into `OrderManager` as `symbol`; Model Blue sizing uses signal legs, not this as the trade universe |
| `candle_timeframe` | `CANDLE_TIMEFRAME` | `"5 mins"` | Parsed by `candle_timeframe_minutes`; **not** used by Model Blue webhook path |
| `strategy_candle_count` | `STRATEGY_CANDLE_COUNT` | `5` | **Not** used by Model Blue webhook path |
| `order_quantity` | `ORDER_QUANTITY` | `1` | Default quantity arg on `OrderManager` |
| `model_blue_committed_notional` | `MODEL_BLUE_COMMITTED_NOTIONAL` | `None` | Temporary paper fallback; OPEN can reject if unset and DB capital missing |
| `paper_execute_stk_as_cfd` | `PAPER_EXECUTE_STK_AS_CFD` | `True` | Requested STK may execute as IBKR CFD on paper; raw signal type stays STK |

Property: `candle_timeframe_minutes` — parses `candle_timeframe` (defaults to 5).

## Demo stream settings (`demo_streaming.config.DemoStreamSettings`)

| Field | Default |
|-------|---------|
| `database_url` | same default Postgres URL as above |
| `redis_url` | `redis://127.0.0.1:6379/0` |
| `demo_stream_host` | `127.0.0.1` |
| `demo_stream_port` | `8010` |
| `demo_poll_interval_ms` | `250` |
| `demo_stream_name` | `positions:stream` |
| `trading_api_url` | `http://127.0.0.1:8000` | Config CRUD proxy target for `:8010` dashboard saves |

## Not in `Settings`

- `BROKER_MODE` — not a field; no MockBroker switch in code.
- `ALLOCATIONS_CONFIG_PATH` — not a field; routing uses Postgres accounts / strategies / allocations.
