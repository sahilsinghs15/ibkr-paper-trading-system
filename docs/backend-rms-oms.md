# RMS, OMS, and IBKR adapter

**Verified from:** `backend/app/rms/engine.py`, `backend/app/rms/checks/*`, `backend/app/oms/basket.py`, `backend/app/oms/coordinator.py`, `backend/app/oms/oms_service.py`, `backend/app/oms/ibkr_adapter.py`, `backend/app/broker/ibkr/gateway_rate_limiter.py`, `backend/app/oms/retry_policy.py`, `backend/app/broker/ibkr/tws_client.py`, `backend/app/accounts/router.py`, `backend/app/accounts/config_service.py`, `backend/app/services/kill_switch.py`, `backend/app/services/order_manager.py`.

## RMS — implemented checks only

`get_default_checks()` order (labels as in engine docstring):

| Order | Label | Class | File |
|-------|-------|-------|------|
| 1 | Check 1 — MARGIN | `MarginCheck` | `rms/checks/margin.py` |
| 2 | Check 2 — DUPLICATE | `DuplicateCheck` | `rms/checks/duplicate.py` |
| 3 | Check 3 — STRATEGY | `StrategyCheck` | `rms/checks/strategy.py` |
| 4 | Check 4 — CONTRACT MONTH | `ContractMonthCheck` | `rms/checks/contract_month.py` |
| 5 | Check 7 — OPEN-POSITION LIMIT | `OpenPositionLimitCheck` | `rms/checks/position_limit.py` |
| 6 | Check 8 — MONEY PER STOCK | `MoneyPerStockCheck` | `rms/checks/money_per_stock.py` |
| 7 | Check 101 — MODEL MARKET VALUE | `ModelMarketValueCheck` | `rms/checks/model_market_value.py` |

Also present: `rms/checks/base.py` (`BaseRMSCheck`), `rms/margin_estimate.py` (band classifier), `rms/market_value.py` (gross notional helpers).

**Not present** as check modules: architecture checks 5, 6, 9.

Check 101 caps **market value**, not broker margin. Ceiling is `total_margin × alloc_pct × market_value_utilisation_cap` keyed `(account_id, strategy_id)`. Legs are summed, never netted. Seeded from `positions` (has `strategy_id`), not `broker_positions`. Ships with `MARKET_VALUE_CHECK_ENABLED=false` (shadow). CLOSE and `EMERGENCY_FLATTEN` always PASS so a full model can still flatten.

Individual pair size is bounded at sizing time by `pair_max_allocation_pct`; check 101 stops N pairs from collectively overrunning the model's allocation.

Check 1 is registered first so a margin rejection beats a duplicate rejection on a replayed signal. Duplicate is the more useful diagnosis on a replay; moving check 1 to second is one line if operators prefer that.

### Check 1 — three-tier band

`MarginCheck` is synchronous: no broker I/O. It classifies `required` vs usable headroom (`effective_free - min_free_buffer`):

| Band | Condition | Order path |
|------|-----------|------------|
| `COMFORTABLE` | `required < usable * comfort_ratio` (default 0.80) | PASS, zero broker calls |
| `BORDERLINE` | otherwise below `usable` | PASS from the check; OrderManager Gate C runs two what-ifs after instrument resolve |
| `INSUFFICIENT` | `required >= usable` | REJECT `MARGIN_INSUFFICIENT` |

`check_enabled=true` (default) enforces check 1. Uncheck on Settings (**Shadow mode**) to PASS with `MARGIN_CHECK_DISABLED`. Flip via `PATCH /api/v1/config/margin` without restarting TWS.

CLOSE and `EMERGENCY_FLATTEN` skip check 1. Missing/stale snapshot fail closed when `reject_on_stale_snapshot` is true.

Gate A (`OrderManager._assert_account_has_free_margin`) runs **before** `build_intent` on OPEN only. Gate C (`_confirm_margin_if_borderline`) runs after resolve; any what-if `inf`/timeout is `MARGIN_PROBE_UNKNOWN` — never fall back to the estimate.

### Short-circuit behavior

`RMSEngine.evaluate` runs checks in fixed order:

- `REJECT` / `HALT` → return immediately
- `ADJUST` + `adjusted_intent` → replace intent for later checks
- All pass → `RMSOutcome.PASS`

### OPEN vs CLOSE / EMERGENCY_FLATTEN

| Check | CLOSE | EMERGENCY_FLATTEN | OPEN |
|-------|-------|-------------------|------|
| 1 Margin | PASS | PASS | Band vs snapshot + tally |
| 2 Duplicate | PASS | — | Reject if in `processed_signals` |
| 3 Strategy | PASS | PASS | Requires strategy in `strategy_configs` |
| 4 Contract month | Expiry instruments only | — | May ADJUST month |
| 7 Position limit | PASS | — | Cap from allocation |
| 8 Money per stock | PASS | PASS | Per-symbol notional cap; unconfigured symbol → `NO_SYMBOL_LIMIT_CONFIGURED` (no $10M default) |
| 101 Model market value | PASS | PASS | Gross MV vs model ceiling |

### DuplicateCheck vs execution claims

- **DuplicateCheck (check 2):** in-memory `processed_signals` — post-success, single-process view
- **execution_claims:** durable barrier acquired **after** RMS + resolve, **before** broker submit — crash-safe across workers

Both must remain; do not remove either without replacing its role.

## Multi-account routing (as-is — PARTIAL)

This is **multi-account on one IB socket**, not multi-Gateway. Target N Gateways: [`backend-multi-gateway.md`](backend-multi-gateway.md).

- `DatabaseStrategyAccountRouter` (`accounts/router.py`) loads rows where `accounts.enabled`, `strategies.enabled`, and `allocations.enabled` are true for the incoming `strategy_id`.
- Builds `AccountExecutionContext` with committed notional from margin × allocation, **pair_budget** = committed × `pair_max_allocation_pct`, and **per-account** `allocations.max_open_positions` (RMS check 7 cap). Fields: `account_id`, `ibkr_account`, `committed_notional`, `pair_budget`, … — **no** host/port/clientId.
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

`PENDING` → `EXECUTING` → `OPEN` / `CLOSED` / `UNWINDING` / `COMPENSATED` / `CRITICAL` / `RECOVERED`

`BasketCoordinator` (`oms/coordinator.py`):

1. Submit N legs via `OMSService.submit_one_leg`
2. Wait for fills (`square_off_after_sec` timeout)
3. If incomplete → retry (including live Gateway 4001) or UNWINDING → compensate → CRITICAL on failure
4. CRITICAL blocks new OPENs for that `(account_id, strategy_id)` until auto-recovery clears the latch
5. `CriticalRecoveryService` (`services/critical_recovery.py`) runs in background after `_fail_critical` and on startup for any remaining CRITICAL rows: `reqPositions` snapshot → `BrokerFlattenService` EMERGENCY_FLATTEN for filled non-compensation `conId`s → fresh snapshot → `clear_critical` only when those lines are ~0 qty
6. `clear_critical` marks the basket `RECOVERED` (`recovery_status=CLEARED`), drops the in-memory latch when no other CRITICAL baskets remain for the pair, emits `BASKET_CRITICAL_CLEARED`
7. Failed recovery sets `recovery_status=FAILED`, emits `BASKET_CRITICAL_RECOVERY_FAILED`, retries once (~30s)

Basket recovery columns on `baskets`: `recovery_status` (`RECOVERING` | `FAILED` | `CLEARED`), `recovery_detail`, `recovered_at`.

### Paper retries

Gated by `ExecutionRetryPolicy` + `paper_retry_ports_allowed(ibkr_port)`:

- Allowed ports: `{7497, 4002, 4001}`
- Live TWS (`7496`) never gets incomplete-leg auto-retry

Knobs in singleton `execution_settings` row; API at `GET/PATCH /api/v1/config/execution`.

## OMS + IBKR

| Component | Role |
|-----------|------|
| `OMSService` | In-memory order lifecycle; `_orders` map; `_submitted_signals` dedup |
| `IBKRExecutionAdapter` | Maps OMS orders ↔ IBKR contracts / `placeOrder` / cancels; sets `ib_order.account` from intent |
| `TWSClient` | Sole broker transport under `app/broker/` (IBKR EClient/EWrapper) |
| `GatewayRateLimiter` | **Production** pacing — token bucket ~30 msg/sec (Settings `IBKR_GATEWAY_*`), P0 flatten reserve, wait+timeout, Error 100 cooldown. Wired in `main.py`. Shared by all accounts and kill-switch flatten. Paces `placeOrder`, `cancelOrder`, `reqMktData` (P3), `reqContractDetails` (P2). |

There is **no** MockBroker class and **no** `BROKER_MODE` switch in `Settings`. **No** per-gateway limiter. **No** connection pool.

### Adapter invariants

- Requires `ResolvedInstrument` on each leg — **no** silent STK/SMART/USD guessing
- Never CFD→STK fallback
- Paper STK→CFD override lives in `instruments/execution_override.py`, not in adapter/TWS/RMS
- `ibkr_account` must be in Gateway `managedAccounts` before `placeOrder` — `TWSClient.managedAccounts` callback populates `managed_accounts` set (waits ≤2s after `nextValidId`); `IBKRExecutionAdapter._validate_ibkr_account` rejects `MISSING_IBKR_ACCOUNT` / `UNMANAGED_ACCOUNT` / not-in-set before pacing

### GatewayRateLimiter (production — single socket)

`broker/ibkr/gateway_rate_limiter.py` — token bucket with global/normal/emergency budgets, priority levels (P0 flatten, P1 orders, P2 contract details, P3 market data), `max_wait_sec` timeout, Error 100 cooldown. `normal = min(configured_normal, max − emergency_reserve)`. P1 (`placeOrder`/`cancelOrder`) may consume without a normal token if that would not eat the P0 reserve. One instance per process today (maps to one Gateway). Per-gateway copies when N-Gateway pool ships — see [`backend-multi-gateway.md`](backend-multi-gateway.md).

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
5. Remainder-retry is allowed on `{7497, 4002, 4001}`. Persist strips `:RETRY:` / `:UNWIND:` into the parent `signals` row; `_open_trade_from_fills` groups by `leg_index`.
6. Compensation / unwind failure → CRITICAL; `CriticalRecoveryService` auto-flattens and unlocks when broker flat — do not SQL-edit or restart to resume OPENs. Disconnect parks the basket (`EXECUTING`); do not compensate.
7. Single TWS socket today — do not add unpaced burst submitters. A second `TWSClient` with the same `client_id` will disconnect the first. N-Gateway pool is target-only. Socket reconnect-on-drop is implemented on that one client.
8. Prefer N-leg `OrderIntent.legs` model throughout OMS/RMS.
9. `(account_id, trade_id)` is unique forever — refuse OPEN if any `positions` row exists, including `CLOSED`.

## Related tests

```bash
cd /home/tradingapp/app/backend
.venv/bin/pytest tests/rms/ tests/test_oms.py tests/test_basket_coordinator.py \
  tests/test_basket_retry.py tests/test_n_leg_execution.py tests/test_kill_switch.py \
  tests/test_pacer.py tests/test_tws_connection.py
```

Full inventory: [`backend-testing.md`](backend-testing.md).
