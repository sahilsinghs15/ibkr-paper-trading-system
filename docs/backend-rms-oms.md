# RMS, OMS, and IBKR adapter

**Verified from:** `backend/app/rms/engine.py`, `backend/app/rms/checks/*`, `backend/app/oms/basket.py`, `backend/app/oms/coordinator.py`, `backend/app/oms/oms_service.py`, `backend/app/oms/ibkr_adapter.py`, `backend/app/broker/ibkr/tws_client.py`, `backend/app/accounts/router.py`, `backend/app/accounts/config_service.py`.

## RMS — implemented checks only

`get_default_checks()` order (labels as in engine docstring):

| Order | Label | Class | File |
|-------|-------|-------|------|
| 1 | Check 2 — DUPLICATE | `DuplicateCheck` | `rms/checks/duplicate.py` |
| 2 | Check 3 — STRATEGY | `StrategyCheck` | `rms/checks/strategy.py` |
| 3 | Check 4 — CONTRACT MONTH | `ContractMonthCheck` | `rms/checks/contract_month.py` |
| 4 | Check 7 — OPEN-POSITION LIMIT | `OpenPositionLimitCheck` | `rms/checks/position_limit.py` |
| 5 | Check 8 — MONEY PER STOCK | `MoneyPerStockCheck` | `rms/checks/money_per_stock.py` |

Also present: `rms/checks/base.py` (`BaseRMSCheck`).

**Not present** as check modules: architecture checks 1 (margin), 5, 6, 9.

`RMSEngine.evaluate` runs the sequence short-circuit style (see engine implementation and `tests/rms/`).

## Multi-account routing

- `DatabaseStrategyAccountRouter` loads rows where `accounts.enabled`, `strategies.enabled`, and `allocations.enabled` are true for the incoming `strategy_id`.
- Builds `AccountExecutionContext` with committed notional from margin × allocation and **per-account** `allocations.max_open_positions` (RMS check 7 cap).
- Fan-out happens inside `OrderManager` (in-process). There is **not** one OS process per IBKR account.
- `AccountStrategyConfigService` enforces allocation uniqueness / sum ≤ 1 for enabled allocations; mounted at `/api/v1/config/*` (see [`backend-api.md`](backend-api.md)).
- Symbol-limit writes call `OrderManager.reload_rms_limits()` so check 8 applies without restart. Allocation % and check 7 caps re-read from Postgres on the next signal.

## Basket atomicity

`BasketState` enum (`oms/basket.py`):

`PENDING` → `EXECUTING` → `OPEN` / `CLOSED` / `UNWINDING` / `COMPENSATED` / `CRITICAL`

`BasketCoordinator` (`oms/coordinator.py`) submits N legs, waits for fills, compensates on failure, and can block new work when baskets are `CRITICAL`. Persists via `baskets` (+ related order / event writes).

## OMS + IBKR

| Component | Role |
|-----------|------|
| `OMSService` | In-memory order lifecycle; submit / cancel; uses adapter |
| `IBKRExecutionAdapter` | Maps OMS orders ↔ IBKR contracts / `placeOrder` / cancels; sets `ib_order.account` from intent |
| `TWSClient` | Sole broker transport under `app/broker/` (IBKR EClient/EWrapper) |

There is **no** MockBroker class and **no** `BROKER_MODE` switch in `Settings`.

## Kill switch / operator controls

- DB flags: `accounts.enabled`, `strategies.enabled`, `allocations.enabled` affect routing; editable on Settings page (`/settings` on `:8010`).
- There is **no** flatten-all HTTP API.
