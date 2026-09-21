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

| Trigger | Endpoint / caller | Scope armed | Flatten performed |
|---|---|---|---|
| Operator — Flatten Signal Positions | `POST /api/v1/config/accounts/{id}/square-off?scope=engine` | `engine` | engine ledger (`positions`) |
| Operator — Flatten Manual Positions | `POST /api/v1/config/accounts/{id}/square-off-manual` | `manual` | manual ledger (`manual_positions`) |
| Operator — Complete Flatten | `POST /api/v1/config/accounts/{id}/square-off-account` | `account` | whole IBKR account, then ledger convergence |
| Emergency webhook | `POST /api/v1/emergency-kill-switch` | `account` | none — arms only |
| Automated risk breach | `RiskExitMonitor` daily target/stop (`requested_by="auto_risk"`) | `account` | engine ledger only |

### Scope Isolation Rule

**A scope blocks only the ledger it flattens.** This is an invariant, not a
convenience:

| Scope | Engine signals | Manual ticket |
|---|---|---|
| `engine` | **BLOCKED** | permitted |
| `manual` | permitted | **BLOCKED** |
| `account` | **BLOCKED** | **BLOCKED** |

`engine` scope must never block manual trading. The engine flatten deliberately
preserves manual positions, so blocking the manual ticket would leave the
operator holding manual exposure with no way to close it.

Use the predicate that matches the ledger being gated:

| Predicate | Gates | True for |
|---|---|---|
| `is_account_kill_switch_active()` | engine OPENs | `engine`, `account` |
| `is_manual_trading_blocked()` | manual order submission | `manual`, `account` |
| `is_account_scope_kill_switch_active()` | — | `account` only |

`is_account_kill_switch_active()` is named for the **account row** it gates, not
for `account` scope. Do not use it to gate manual trading.

Each scope has its own hot cache, and every cache must be reconstructable by
`hydrate_kill_switch_cache()` from its database predicate. Clear them through
`clear_account_kill_switch_cache()`, never by discarding from one set directly.

**Automatic risk breach blocks but does not auto-flatten manual lots.** Placing
broker orders against operator-owned positions remains an explicit operator
action (§7 of `AGENTS.md`).

### Reconciliation Evidence Rule

A manual lot is resolved from one of two things, in order:

1. **Exact linkage** — a `ks_manual` close order at `FILLED` whose
   `manual_executions` rows cover the snapshotted quantity.
2. **Per-symbol broker comparison** — fallback when the order never reached a
   terminal status.

Route 2 compares a `broker_positions` snapshot (stamped `as_of`) against live
`positions` state. Because manual scope does not block engine signals, those two
can describe different moments on a shared symbol. It is trusted **only** when:

- `max(broker_positions.as_of) >= ` the close order's `submitted_at` (falling
  back to the operation's `created_at`); **and**
- no `positions` row on that symbol has `opened_at` or `closed_at` at or after
  `as_of`.

Otherwise the lot is left unresolved and retried. **Never mutate position status
from a comparison spanning two instants** — marking a live lot
`FLATTENED_PENDING_PRICE` reports it as flat when it is not.

### The Armed Invariant
- Activating the kill switch creates a row in `kill_switch_operations` with status `ACTIVATING` and sets `_KILL_SWITCH_ACTIVE_ACCOUNTS`.
- Completing the flatten moves status to `COMPLETE` or `FLAT`.
- **CRITICAL**: Statuses `COMPLETE` and `FLAT` are STILL CONSIDERED ARMED (`_ARMED_STATUSES`). The account **REMAINS BLOCKED** from opening new positions across crashes and restarts.
- **Disarming Rule**: An account is ONLY disarmed when an operator explicitly invokes `POST /api/v1/config/accounts/{id}/kill-switch/clear`, which transitions the operation to `CLEARED`. The endpoint takes no scope argument and clears **every** scope for the account; `clear_account_kill_switch(..., scope=...)` can narrow it, but no route exposes that today.
- **Manual scope is the exception**: it is released automatically when its operation reaches a terminal status (`COMPLETE` **or** `UNRESOLVED`), because an unresolved manual flatten is precisely the state that needs the operator to place manual closes.

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
- **Exception**: Position-reducing or closing orders (sells for longs, buys for shorts) are **PERMITTED** while paused to allow operators to de-risk. An armed kill switch has **no** such exemption — once armed, the flatten owns the position.
- **Interaction**: A paused account is skipped by `RiskExitMonitor`'s account-level daily-risk evaluation, so the automatic kill switch will not arm while paused. Per-pair exits still run.
