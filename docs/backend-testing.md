# Backend testing

**Verified from:** `backend/pyproject.toml`, `backend/tests/**/test_*.py` module docstrings, `backend/docs/DEVELOPER_EXECUTION_GUIDE.md` commands.

## Commands

From `app/backend` (package name `backend`, Python `>=3.12`):

```bash
cd /home/tradingapp/app/backend
.venv/bin/pytest
.venv/bin/ruff check app/ tests/ scripts/
```

Pytest config in `pyproject.toml`: `testpaths = ["tests"]`, `asyncio_mode = "auto"`.

Dev extras: `httpx`, `mypy`, `pytest`, `pytest-asyncio`, `ruff` (`[project.optional-dependencies] dev`).

`conftest.py` defaults `PAPER_EXECUTE_STK_AS_CFD=false` for tests (overrides the production Settings default of `True`).

## Test files (34) → intent from docstrings / names

| File | Intent (from module docstring) |
|------|--------------------------------|
| `test_api.py` | Health, webhooks, orders, lifespan |
| `test_app_wiring.py` | DI / component wiring |
| `test_tradingview_webhook.py` | TradingView webhook API |
| `test_tradingview_execution_integration.py` | Webhook → sizer → RMS → OMS → IBKR adapter |
| `test_tradingview_signal_persistence.py` | Signal persistence |
| `test_signal_payload_persistence.py` | Webhook JSON / pair / side audit |
| `test_model_blue.py` | Model Blue parser and sizer |
| `test_order_manager.py` | OrderManager facade |
| `test_oms.py` | OMS + IBKR adapter (offline) |
| `test_basket_coordinator.py` | Basket atomicity (mocked IBKR) |
| `test_n_leg_execution.py` | N-leg RMS/OMS vs Model Blue isolation |
| `test_multi_account_routing.py` | Strategy → eligible accounts |
| `test_production_path_hardening.py` | Production-path gaps (mocked IBKR) |
| `test_hardening_lifecycle.py` | Quantity, RMS, persist, P&L, CLOSE |
| `test_db_model_blue_persistence.py` | Model Blue trades across sessions |
| `test_execution_audit_persistence.py` | Execution ledger / fill precision |
| `test_persistent_schema.py` | Persistent schema unit tests |
| `test_database.py` | SQLAlchemy / DB connection |
| `test_config.py` | Configuration |
| `test_logger.py` | Logging infrastructure |
| `test_models.py` | Domain models |
| `test_instrument_resolution.py` | Instrument / contract resolution |
| `test_stk_to_cfd_demo_override.py` | Paper STK→CFD override |
| `test_demo_streaming.py` | Demo streaming helpers (no IBKR) |
| `test_tws_connection.py` | TWSClient lifecycle |
| `test_instrument_master_discover.py` | Discover script (mocked) |
| `test_seed_fetcher.py` | NASDAQ seed fetcher fixtures |
| `test_pacer.py` | RatePacer token bucket |
| `rms/test_rms_engine.py` | RMSEngine orchestration |
| `rms/test_rms_duplicate.py` | Check 2 |
| `rms/test_rms_strategy.py` | Check 3 |
| `rms/test_rms_contract_month.py` | Check 4 |
| `rms/test_rms_position_limit.py` | Check 7 |
| `test_config_service.py` | AccountStrategyConfigService validation |
| `test_config_api.py` | Config CRUD HTTP + RMS limit reload |

This list is an inventory of test modules, not a claim about line coverage.
