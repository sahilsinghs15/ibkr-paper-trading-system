# 10-RISK-SAFETY.md — Risk Management System & Safety Controls

---

## 1. RMS (RISK MANAGEMENT SYSTEM) PIPELINE

Engine algorithmic orders must pass sequential checks in [`backend/app/rms/engine.py`](file:///home/dev3/Documents/ibkr-paper-trading-system/backend/app/rms/engine.py#L43):

| Check # | Name | Check Class | Evaluated Invariant | Action on Failure |
|---|---|---|---|---|
| **1** | Margin Check | `MarginCheck` | Estimated order initial margin impact $\le$ available account margin. | `REJECT` |
| **2** | Duplicate Check | `DuplicateCheck` | Intent `(strategy_id, signal_id)` has not been previously processed. | `REJECT` |
| **3** | Strategy Check | `StrategyCheck` | Strategy exists, is enabled, and is allowed on the account. | `REJECT` |
| **4** | Contract Month | `ContractMonthCheck`| Validates futures expiry month if trading derivatives. | `REJECT` |
| **7** | Position Limit | `OpenPositionLimitCheck`| Open positions on the account $\le$ `max_open_positions`. | `REJECT` |
| **8** | Money Per Stock | `MoneyPerStockCheck` | Account notional exposure per symbol $\le$ `per_symbol_limits.money_limit`. | `ADJUST` or `REJECT` |
| **101** | Model Market Value| `ModelMarketValueCheck`| Total portfolio market value $\le$ committed capital utilization cap. | `REJECT` |

---

## 2. EMERGENCY KILL SWITCH

The Kill Switch ([`backend/app/services/kill_switch.py`](file:///home/dev3/Documents/ibkr-paper-trading-system/backend/app/services/kill_switch.py#L125)) is the master emergency position flattening system.

### Trigger Sources
1. **Operator Trigger**: `POST /api/v1/config/accounts/{id}/kill-switch`
2. **Automated Risk Breach**: Triggered by `RiskExitMonitor` when an account's daily loss threshold is breached (`requested_by="auto_risk"`).

### Flattening Scopes
- `ENGINE_POSITIONS`: Liquidates all algorithmic pair positions in `positions`.
- `ALL_POSITIONS`: Liquidates all engine positions AND all manual positions (`manual_positions`).

### The Armed Invariant
- Activating the kill switch creates a row in `kill_switch_operations` with status `ACTIVATING` and sets `_KILL_SWITCH_ACTIVE_ACCOUNTS`.
- Completing the flatten moves status to `COMPLETE` or `FLAT`.
- **CRITICAL**: Statuses `COMPLETE` and `FLAT` are STILL CONSIDERED ARMED (`_ARMED_STATUSES`). The account **REMAINS BLOCKED** from opening new positions across crashes and restarts.
- **Disarming Rule**: An account is ONLY disarmed when an operator explicitly invokes `POST /api/v1/config/accounts/{id}/kill-switch/clear`, which transitions the operation to `CLEARED`.

---

## 3. GLOBAL RED ZONE GATING (`SessionClock`)

- **Authoritative Calendar**: Built on `exchange_calendars` for NYSE (XNYS).
- **Red Zone Window**: `[session_close - buffer, session_close)` (default buffer: 15 minutes before 16:00 ET).
- **Projected Check**: Evaluates whether `current_time + gateway_max_wait` enters the Red Zone.
- **Action**: Algorithmic orders entering the red zone are parked in `signal_jobs` with status `DEFERRED_RED_ZONE`.
- **Release**: `RedZoneReleaseService` automatically releases deferred jobs when RTH opens the next trading day.

---

## 4. ACCOUNT TRADING PAUSE

- Configured per account via `accounts.trading_paused` in PostgreSQL and cached in memory.
- **Rule**: Pausing an account blocks all new `OPEN` orders.
- **Exception**: Position-reducing or closing orders (sells for longs, buys for shorts) are **PERMITTED** while paused to allow operators to de-risk.
