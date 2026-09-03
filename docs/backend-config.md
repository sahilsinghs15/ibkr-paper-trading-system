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
| `jwt_secret_key` | `JWT_SECRET_KEY` | placeholder | Must be replaced before serving HTTP |
| `jwt_algorithm` | `JWT_ALGORITHM` | `HS256` | |
| `jwt_access_token_expire_minutes` | `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | `480` | |
| `ibkr_host` | `IBKR_HOST` | `"127.0.0.1"` | The **only** TWS/Gateway host. No per-account override. |
| `ibkr_port` | `IBKR_PORT` | `4001` | Live Gateway. The only production port. |
| `ibkr_client_id` | `IBKR_CLIENT_ID` | `1` | The **only** API client id. Duplicate client ids on the same Gateway disconnect the older session. |
| `ibkr_connection_timeout` | `IBKR_CONNECTION_TIMEOUT` | `10` | Seconds |
| `ibkr_gateway_max_msg_per_sec` | `IBKR_GATEWAY_MAX_MSG_PER_SEC` | `30` | Global token-bucket ceiling (headroom under IB ~50 msg/sec) |
| `ibkr_gateway_normal_msg_per_sec` | `IBKR_GATEWAY_NORMAL_MSG_PER_SEC` | `24` | Normal workload budget (P1–P4) |
| `ibkr_gateway_emergency_reserve_per_sec` | `IBKR_GATEWAY_EMERGENCY_RESERVE_PER_SEC` | `6` | P0 flatten reserve. Honoured: `normal = min(configured, max − reserve)`. P1 orders may use leftover tokens without consuming the reserve. |
| `ibkr_gateway_max_wait_sec` | `IBKR_GATEWAY_MAX_WAIT_SEC` | `8` | Max wait before pacing timeout (no IB send) |
| `ibkr_gateway_error100_cooldown_sec` | `IBKR_GATEWAY_ERROR100_COOLDOWN_SEC` | `2` | Backoff after IB Error 100 |
| `min_order_notional` | `MIN_ORDER_NOTIONAL` | `100` | Sizer rejects any leg below this notional |
| `pair_ratio_tolerance` | `PAIR_RATIO_TOLERANCE` | `0.5` | Post-rounding share-of-pair drift (wide until logs inform a tighter value) |
| `pair_min_deployment_pct` | `PAIR_MIN_DEPLOYMENT_PCT` | `0` | 0 disables the under-deployment floor |
| `market_value_utilisation_cap` | `MARKET_VALUE_UTILISATION_CAP` | `1.0` | Multiplies the model MV ceiling. Defaults to 1.0 because `pair_max_allocation_pct` is the finer-grained control; keep as a global emergency tightener |
| `market_value_check_enabled` | `MARKET_VALUE_CHECK_ENABLED` | `false` | RMS check 101 shadow mode until flipped |
| `margin_whatif_enabled` | `MARGIN_WHATIF_ENABLED` | `false` | Infra: allow `placeOrder(whatIf=True)`. Ships dark |
| `margin_whatif_timeout_sec` | `MARGIN_WHATIF_TIMEOUT_SEC` | `5.0` | What-if wait; then `unknown` |
| `margin_scan_enabled` | `MARGIN_SCAN_ENABLED` | `false` | Infra: background/startup rate scanner |
| `margin_scan_max_per_sec` | `MARGIN_SCAN_MAX_PER_SEC` | `5.0` | Scanner private token bucket (not the gateway limiter) |
| `margin_scan_startup_budget_sec` | `MARGIN_SCAN_STARTUP_BUDGET_SEC` | `20.0` | First-scan wall clock; remainder deferred |
| `margin_scan_probe_notional` | `MARGIN_SCAN_PROBE_NOTIONAL` | `1000` | Probe size for rate = initMarginChange / notional |
| `margin_scan_signal_lookback_days` | `MARGIN_SCAN_SIGNAL_LOOKBACK_DAYS` | `30` | Working-set lookback |
| `margin_rate_max_age_days` | `MARGIN_RATE_MAX_AGE_DAYS` | `7` | Stale `margin_rates` rows ignored |
| `margin_rate_refresh_sec` | `MARGIN_RATE_REFRESH_SEC` | `300` | Background scan interval |
| `margin_snapshot_max_age_sec` | `MARGIN_SNAPSHOT_MAX_AGE_SEC` | `300` | Snapshot staleness |
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
| `execute_stk_as_cfd` | `EXECUTE_STK_AS_CFD` (alias `PAPER_EXECUTE_STK_AS_CFD`) | `True` | Production Model Blue: requested STK executes as IBKR CFD; raw signal type stays STK |
| `webhook_auth_secret` | `WEBHOOK_AUTH_SECRET` | `None` | Required when `webhook_auth_enabled=true`; ingest refuses to start if missing |
| `webhook_auth_enabled` | `WEBHOOK_AUTH_ENABLED` | `True` | Fail-closed: enabled + unset secret → 401 / refuse boot |
| `emergency_killswitch_auth_secret` | `EMERGENCY_KILLSWITCH_AUTH_SECRET` | `None` | Bearer secret for `POST /api/v1/emergency-kill-switch` |
| `emergency_killswitch_auth_enabled` | `EMERGENCY_KILLSWITCH_AUTH_ENABLED` | `True` | Toggle emergency kill-switch auth |

Property: `candle_timeframe_minutes` — parses `candle_timeframe` (defaults to 5).

Runtime guard: `TRADINGAPP_TESTING=1` is pytest-only. `get_settings()` refuses production database `ibkr_trading` under that flag. `trading-backend` (any `placeOrder` process) refuses the flag at startup unless pytest is running. Never set it in systemd `Environment=` / `EnvironmentFile`.

### Infra vs operator policy

Gateway pacing and what-if/scanner switches live in **env** (`Settings`) because they are process/socket concerns. Margin **gate policy** (`check_enabled` default **true**, `gate_basis`, `comfort_ratio`, floors, look-ahead, `default_rate`) lives in the `margin_settings` singleton and is edited via `GET/PATCH /api/v1/config/margin`. Flipping `check_enabled` must not restart `app.main` (that drops the TWS socket). Same pattern as `execution_settings`.

## Demo stream settings (`demo_streaming.config.DemoStreamSettings`)

| Field | Default |
|-------|---------|
| `database_url` | same default Postgres URL as above |
| `redis_url` | `redis://127.0.0.1:6379/0` |
| `demo_stream_host` | `127.0.0.1` |
| `demo_stream_port` | `8010` |
| `demo_poll_interval_ms` | `2000` |
| `demo_signal_watch_limit` | `500` |
| `demo_pnl_emit_interval_ms` | `5000` |
| `demo_stream_maxlen` | `10000` |
| `demo_stream_name` | `positions:stream` |
| `trading_api_url` | `http://127.0.0.1:8001` | Config CRUD proxy target for `:8010` dashboard saves |

## Not in `Settings`

- `BROKER_MODE` — not a field; no MockBroker switch in code.
- `ALLOCATIONS_CONFIG_PATH` — not a field; routing uses Postgres accounts / strategies / allocations.
- Worker pool size (`10`), job lease durations, reclaim intervals — hardcoded in `main.py` / `worker_pool.py`. See [`backend-map.md`](backend-map.md).
- Gateway pool / per-account `IBKR_HOST` — **not fields**. `accounts.ibkr_account` is the IB account string tagged on `placeOrder`, not a socket. Target schema: [`backend-multi-gateway.md`](backend-multi-gateway.md).
