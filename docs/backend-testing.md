# Backend testing

**Verified from:** `backend/pyproject.toml`, `backend/tests/**/test_*.py`, `backend/docs/DEVELOPER_EXECUTION_GUIDE.md` commands.

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

## Test files (47) → intent

| File | Intent |
|------|--------|
| `test_api.py` | Health, webhooks, orders, lifespan |
| `test_app_wiring.py` | DI / component wiring |
| `test_tradingview_webhook.py` | TradingView webhook API (202 enqueue) |
| `test_tradingview_execution_integration.py` | Webhook → sizer → RMS → OMS → IBKR adapter |
| `test_tradingview_signal_persistence.py` | Signal persistence |
| `test_signal_payload_persistence.py` | Webhook JSON / pair / side audit |
| `test_model_blue.py` | Model Blue parser and sizer |
| `test_order_manager.py` | OrderManager facade |
| `test_oms.py` | OMS + IBKR adapter (offline) |
| `test_basket_coordinator.py` | Basket atomicity (mocked IBKR) |
| `test_basket_retry.py` | Paper retry policy + submit pacer |
| `test_n_leg_execution.py` | N-leg RMS/OMS vs Model Blue isolation |
| `test_naked_pair_protection_fix.py` | Naked pair protection |
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
| `test_cfd_discover.py` | CFD discover (mocked) |
| `test_demo_streaming.py` | Demo streaming helpers (no IBKR) |
| `test_demo_streaming_signal_persistence.py` | Demo signal display / persistence |
| `test_tws_connection.py` | TWSClient lifecycle |
| `test_market_data_pipeline.py` | Live PnL / market data |
| `test_instrument_master_discover.py` | Discover script (mocked) |
| `test_seed_fetcher.py` | NASDAQ seed fetcher fixtures |
| `test_pacer.py` | `scripts.instrument_master.pacer.RatePacer` (discover CLI) — **not** `OrderSubmitPacer` |
| `test_kill_switch.py` | Kill switch service + EMERGENCY_FLATTEN RMS bypass |
| `test_kill_switch_reconciliation_fix.py` | Kill switch position reconciliation |
| `test_repair_historical_killswitch_positions.py` | Historical kill-switch repair script |
| `test_mft_concurrency_recovery.py` | Worker pool + IBKRExecutionScheduler (scheduler tests-only) |
| `test_burst_stress_150_300.py` | Burst stress (150/300 webhooks) |
| `test_burst_stress_500_and_kill_switch.py` | Burst stress + kill switch under load |
| `rms/test_rms_engine.py` | RMSEngine orchestration |
| `rms/test_rms_duplicate.py` | Check 2 |
| `rms/test_rms_strategy.py` | Check 3 |
| `rms/test_rms_contract_month.py` | Check 4 |
| `rms/test_rms_position_limit.py` | Check 7 |
| `rms/test_rms_money_per_stock.py` | Check 8 |
| `test_config_service.py` | AccountStrategyConfigService validation |
| `test_config_api.py` | Config CRUD HTTP + RMS limit reload |

## Suggested runs by area

| Area | Command |
|------|---------|
| RMS | `.venv/bin/pytest tests/rms/` |
| OMS / basket | `.venv/bin/pytest tests/test_oms.py tests/test_basket_coordinator.py tests/test_basket_retry.py` |
| Concurrency / claims | `.venv/bin/pytest tests/test_mft_concurrency_recovery.py tests/test_tradingview_webhook.py` |
| Kill switch | `.venv/bin/pytest tests/test_kill_switch.py tests/test_kill_switch_reconciliation_fix.py` |
| Full integration (mocked IBKR) | `.venv/bin/pytest tests/test_tradingview_execution_integration.py tests/test_hardening_lifecycle.py` |
| Stress (heavy) | `.venv/bin/pytest tests/test_burst_stress_150_300.py tests/test_burst_stress_500_and_kill_switch.py` |

Many tests use mocked IBKR and do not require a live TWS connection. Tests touching Postgres need `DATABASE_URL` (default in Settings). Burst stress tests are slow.

This list is an inventory of test modules, not a claim about line coverage.

## Operational scripts (need a running app, not pytest)

| Script | Role |
|--------|------|
| `scripts/load_test_mft_burst.py` | Burst N webhooks at a live app; reports ack rate, latency percentiles, and with `--audit` the resulting `signal_jobs` statuses |
| `scripts/prune_webhook_captures.py` | Delete raw captures under `data/tradingview_webhooks/` past a retention window; dry run unless `--apply` |
| `scripts/repair_historical_killswitch_positions.py` | One-time repair of stale kill-switch positions |

```bash
.venv/bin/python scripts/load_test_mft_burst.py --count 150 --audit
.venv/bin/python scripts/prune_webhook_captures.py --days 14 --apply
```

The load-test script replaces the earlier `scratch/stress_webhook_150.py` and `scratch/stress_webhook_300.py`, both of which were deleted. The pytest burst-stress modules above cover the same ground in-process.
