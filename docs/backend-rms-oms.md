# RMS, OMS, and IBKR adapter

**Verified from:** `backend/app/rms/engine.py`, `backend/app/rms/checks/*`, `backend/app/oms/basket.py`, `backend/app/oms/coordinator.py`, `backend/app/oms/oms_service.py`, `backend/app/oms/ibkr_adapter.py`, `backend/app/oms/submit_pacer.py`, `backend/app/oms/retry_policy.py`, `backend/app/broker/ibkr/tws_client.py`, `backend/app/broker/ibkr/scheduler.py`, `backend/app/accounts/router.py`, `backend/app/accounts/config_service.py`, `backend/app/services/kill_switch.py`, `backend/app/services/order_manager.py`.

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

### Short-circuit behavior

`RMSEngine.evaluate` runs checks in fixed order:

- `REJECT` / `HALT` → return immediately
- `ADJUST` + `adjusted_intent` → replace intent for later checks
- All pass → `RMSOutcome.PASS`

### OPEN vs CLOSE / EMERGENCY_FLATTEN

| Check | CLOSE | EMERGENCY_FLATTEN | OPEN |
|-------|-------|-------------------|------|
| 2 Duplicate | PASS | — | Reject if in `processed_signals` |
| 3 Strategy | PASS | PASS | Requires strategy in `strategy_configs` |
| 4 Contract month | Expiry instruments only | — | May ADJUST month |
| 7 Position limit | PASS | — | Cap from allocation |
| 8 Money per stock | PASS | PASS | Per-symbol notional cap |

### DuplicateCheck vs execution claims

- **DuplicateCheck (check 2):** in-memory `processed_signals` — post-success, single-process view
- **execution_claims:** durable barrier acquired **after** RMS + resolve, **before** broker submit — crash-safe across workers

Both must remain; do not remove either without replacing its role.

## Multi-account routing (as-is — PARTIAL)

This is **multi-account on one IB socket**, not multi-Gateway. Target N Gateways: [`backend-multi-gateway.md`](backend-multi-gateway.md).

- `DatabaseStrategyAccountRouter` (`accounts/router.py`) loads rows where `accounts.enabled`, `strategies.enabled`, and `allocations.enabled` are true for the incoming `strategy_id`.
- Builds `AccountExecutionContext` with committed notional from margin × allocation and **per-account** `allocations.max_open_positions` (RMS check 7 cap). Fields: `account_id`, `ibkr_account`, `committed_notional`, … — **no** host/port/clientId.
- Fan-out: `OrderManager._fanout_accounts` (`asyncio.gather`). There is **not** one OS process per IBKR account and **not** one `TWSClient` per account.
- Broker identity: `IBKRExecutionAdapter._build_ibkr_order` sets `ib_order.account = intent.ibkr_account`. That only works when the **connected Gateway login** is authorized for those account ids (same paper user or FA master). Independent IB logins need separate Gateway processes — **MISSING**.
- `AccountStrategyConfigService` enforces allocation uniqueness / sum ≤ 1 for enabled allocations; mounted at `/api/v1/config/*`.
- Symbol-limit writes call `OrderManager.reload_rms_limits()` so check 8 applies without restart.

## Kill switch vs RMS enabled flags

There is **no** global RMS on/off flag.

| Control | Effect |
|---------|--------|
| `accounts.enabled` / `strategies.enabled` / `allocations.enabled` | Router eligibility only |
| Kill switch armed | Blocks **OPEN** in fan-out before RMS; CLOSE/flatten still run |
| `execution_settings.enabled` | Basket retry / auto square-off only — not RMS |
| Basket CRITICAL | Blocks OPEN for `(account_id, strategy_id)` |

See [`backend-kill-switch.md`](backend-kill-switch.md) for flatten API and armed/cleared semantics.

## Basket atomicity

`BasketState` enum (`oms/basket.py`):

`PENDING` → `EXECUTING` → `OPEN` / `CLOSED` / `UNWINDING` / `COMPENSATED` / `CRITICAL`

`BasketCoordinator` (`oms/coordinator.py`):

1. Submit N legs via `OMSService.submit_one_leg`
2. Wait for fills (`square_off_after_sec` timeout)
3. If incomplete → retry (paper ports only) or UNWINDING → compensate → CRITICAL on failure
4. CRITICAL blocks new OPENs for that `(account_id, strategy_id)`

### Paper retries

Gated by `ExecutionRetryPolicy` + `paper_retry_ports_allowed(ibkr_port)`:

- Allowed ports: `{7497, 4002}` only
- Live ports (`7496`, `4001`) never get incomplete-leg auto-retry

Knobs in singleton `execution_settings` row; API at `GET/PATCH /api/v1/config/execution`.

## OMS + IBKR

| Component | Role |
|-----------|------|
| `OMSService` | In-memory order lifecycle; `_orders` map; `_submitted_signals` dedup |
| `IBKRExecutionAdapter` | Maps OMS orders ↔ IBKR contracts / `placeOrder` / cancels; sets `ib_order.account` from intent |
| `TWSClient` | Sole broker transport under `app/broker/` (IBKR EClient/EWrapper) |
| `OrderSubmitPacer` | **Production** pacing — min 0.2s between `placeOrder` calls (wired in `main.py`). Process-local `asyncio.Lock`. Shared by **all** accounts and kill-switch flatten. Does **not** pace `reqMktData` / `reqContractDetails` / `cancelOrder`. Waits forever (no timeout, no reject). |

There is **no** MockBroker class and **no** `BROKER_MODE` switch in `Settings`. **No** per-gateway limiter. **No** connection pool.

### Adapter invariants

- Requires `ResolvedInstrument` on each leg — **no** silent STK/SMART/USD guessing
- Never CFD→STK fallback
- Paper STK→CFD override lives in `instruments/execution_override.py`, not in adapter/TWS/RMS

### IBKRExecutionScheduler (tests-only — ASPIRATIONAL shape)

`broker/ibkr/scheduler.py` defines token-bucket priority scheduling (`PRIORITY_EMERGENCY_FLATTEN = 0`, etc.), comments Error 100 = 50 msg/sec, and splits 30/24/6 with emergency reserve. It is **not wired** in `main.py` or the adapter. Only exercised in `tests/test_mft_concurrency_recovery.py`. Do not document it as live submit pacing.

Target: wire **one scheduler/limiter per Gateway instance**, not one global scheduler for N Gateways. See [`backend-multi-gateway.md`](backend-multi-gateway.md).

## Execution settings

**Model:** `ExecutionSettingsModel` — singleton `id=1`:

| Field | Default | Role |
|-------|---------|------|
| `enabled` | true | Master switch for basket retries |
| `square_off_after_sec` | 30 | Fill wait timeout |
| `max_retries` | 3 | Incomplete-leg retry count |
| `retry_interval_sec` | 5 | Between retries |
| `retry_window_sec` | 30 | Total retry window |

`PATCH /api/v1/config/execution` → `OrderManager.reload_execution_policy()` → `BasketCoordinator.apply_retry_policy`.

## Live PnL

`LivePnlService` (`services/pnl.py`):

- Subscribes IBKR market data for open legs via `TWSClient.reqMktData`
- Mark = last → mid(bid/ask) → close; never uses entry as mark
- Persists `positions.live_pnl` with coalescing: at most one in-flight write per trade, minimum 1s between successful persists for the same `(account_id, trade_id)`, skips DB write when pnl is unchanged; first mark may persist immediately after hydrate
- Hydrate on startup after TWS connect
- Health via `get_market_data_health()` (exposed on demo `:8010/demo/market-data-health`)

## Hard invariants for agents

1. RMS check order is fixed — do not reorder without updating tests/docs.
2. CLOSE / EMERGENCY_FLATTEN must stay unblocked by duplicate / strategy / money / open-limit checks.
3. CRITICAL and kill-switch block OPEN only — not CLOSE safety.
4. Claim after RMS, before broker — see [`backend-concurrency.md`](backend-concurrency.md).
5. Paper retries only on ports `{7497, 4002}`.
6. Compensation / unwind failure → CRITICAL; reconcile before new OPENs.
7. Single TWS socket today — do not add unpaced burst submitters. A second `TWSClient` with the same `client_id` will disconnect the first. N-Gateway pool is target-only.
8. Prefer N-leg `OrderIntent.legs` model throughout OMS/RMS.

## Related tests

```bash
cd /home/tradingapp/app/backend
.venv/bin/pytest tests/rms/ tests/test_oms.py tests/test_basket_coordinator.py \
  tests/test_basket_retry.py tests/test_n_leg_execution.py tests/test_kill_switch.py \
  tests/test_pacer.py tests/test_tws_connection.py
```

Full inventory: [`backend-testing.md`](backend-testing.md).
