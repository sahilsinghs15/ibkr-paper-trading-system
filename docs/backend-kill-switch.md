# Kill switch — emergency flatten and OPEN block

**Verified from:** `backend/app/services/kill_switch.py`, `backend/app/db/models/kill_switch.py`, `backend/app/api/routes/config.py`, `backend/app/services/order_manager.py`, `backend/app/rms/checks/strategy.py`, `backend/app/rms/checks/money_per_stock.py`, `backend/app/rms/models.py`, `backend/app/oms/coordinator.py`, `backend/scripts/oms/flatten_gateway_positions.py`.

Operator emergency subsystem: flatten all open positions for an account, block new OPEN signals until explicitly cleared. Distinct from `accounts.enabled` / allocation flags (those affect routing only).

## HTTP API

Mounted under `/api/v1/config/accounts/{account_id}/...` in `api/routes/config.py` and `/api/v1/emergency-kill-switch` in `api/routes/emergency.py`:

| Method | Path | Status | Role |
|--------|------|--------|------|
| `POST` | `/api/v1/config/accounts/{account_id}/square-off` | 202 | Start EC2 emergency flatten (background worker task) |
| `GET` | `/api/v1/config/accounts/{account_id}/kill-switch` | 200 | Report whether account is armed (blocks OPEN) |
| `POST` | `/api/v1/config/accounts/{account_id}/kill-switch/clear` | 200 | Disarm account — only way to resume OPENs (Start Again) |
| `POST` | `/api/v1/emergency-kill-switch` | 200 | External pre-flight webhook: arms existing Kill Switch (NO broker flatten on EC2) |

Proxied from demo dashboard `:8010` via `/api/v1/config/*` proxy.

### External Emergency Pre-Flight Webhook

Mounted at `POST /api/v1/emergency-kill-switch`:
- **Auth**: `Authorization: Bearer <EMERGENCY_KILLSWITCH_AUTH_SECRET>` (constant-time verification; fails closed 401 if secret unconfigured)
- **Payload**: `{"ibkr_account_id": "DU1234567"}`
- **Behavior**: Resolves `ibkr_account_id` string to internal `account_id` and arms the **SAME** existing account Kill Switch state (`_KILL_SWITCH_ACTIVE_ACCOUNTS` and PostgreSQL `kill_switch_operations`).
- **NO BROKER EXECUTION**: The webhook does **NOT** submit IBKR orders, call OMS, or run position flattening on EC2. The local Emergency Kill Switch system handles the actual IBKR broker position flattening. If EC2 is unreachable, the local emergency system proceeds directly with its own broker flatten.
- **Idempotency**: Repeated requests return HTTP 200 with `"message": "Kill switch was already active for account"`.
- **Start Again**: Disarmed using the standard `POST /api/v1/config/accounts/{account_id}/kill-switch/clear` endpoint.

### Square-off response

Returns `SquareOffResponse` with `operation_id`, `status`, `squared_off_count` (= initial open position count). Duplicate square-off **while armed** (including `COMPLETE` / `UNRESOLVED`) is refuse — HTTP returns the existing operation with `created_new=False`. Only after Start Again (`CLEARED`) may a new flatten start. Partial unique index `uq_kill_switch_operations_armed_account` enforces one armed row per account.

## Armed vs cleared

Two separate concepts agents must not conflate:

| Concept | Meaning |
|---------|---------|
| **Armed** | Account blocked from new OPEN signals |
| **Flatten complete** | All positions closed (or marked UNRESOLVED) |

Completing flatten (`COMPLETE` / `UNRESOLVED`) **does not** disarm. Only `POST .../kill-switch/clear` moves operations to `CLEARED` and removes the account from the armed cache.

### In-memory cache

`_KILL_SWITCH_ACTIVE_ACCOUNTS: set[int]` in `kill_switch.py`.

- Rebuilt from Postgres on every `hydrate_runtime_from_db()` via `hydrate_kill_switch_cache`
- **Must** run before any signal is processed — restart without hydrate would silently disarm
- Never mutate the set directly; use `_arm_kill_switch_cache` / `clear_account_kill_switch`

### Armed statuses (DB)

Operations in these statuses leave the account armed:

`ACTIVATING`, `FLATTENING`, `RECONCILING`, `RETRYING`, `FLAT`, `COMPLETE`, `UNRESOLVED`

Only `CLEARED` is terminal-and-disarmed.

## Operation status machine

```
ACTIVATING → FLATTENING → RECONCILING / RETRYING
  → COMPLETE (all flat) | UNRESOLVED (remaining exposure)
  → CLEARED (operator explicit clear only)
```

**Model:** `KillSwitchOperationModel` in `db/models/kill_switch.py`, table `kill_switch_operations`.

Alembic HEAD adds `cleared_at`, `cleared_by` (revision `b6d8f0a2c147`).

## Pipeline interaction

### OPEN block

In `OrderManager._fanout_single_account`:

```python
if intent.action == OrderAction.OPEN and is_account_kill_switch_active(ctx.account_id):
    # reject — KILL_SWITCH_ACTIVE
```

CLOSE signals and kill-switch flatten itself are **not** blocked by the armed cache.

### Flatten execution

`KillSwitchService._execute_flatten_operation`:

1. Load OPEN positions for account
2. For each position (bounded parallel, semaphore 5):
   - Build reverse legs from `leg_a_signed_qty` / `leg_b_signed_qty`
   - Create `OrderIntent` with `ExecutionIntentMode.EMERGENCY_FLATTEN`
   - Synthetic RMS PASS (`reason="KILL_SWITCH_EMERGENCY_CLOSE"`)
   - `BasketCoordinator.execute(..., order_type="MARKET")`
3. Reconcile: auto-close stale OPEN rows whose close orders filled in DB
4. Finalize operation → `COMPLETE` or `UNRESOLVED`

On full fill: persist `POSITION_CLOSE` via `PositionRepository.close_trade` + `EventRepository.append`. Incomplete `exit_marks` or close-qty ≠ open signed qty refuses the close (row stays OPEN).

Flatten paths **deliberately skip `execution_claims`**. Mutual exclusion is `flatten_inflight` keys (`ledger_key` / `broker_key`) shared by kill-switch, pair-close, and broker leftover flatten. A second producer gets 409 / already-flattening rather than a second `placeOrder`. Compensation close orders are not counted as flatten fills.

Flatten tasks are stored (`KillSwitchService._in_flight`) and resumed on hydrate for `ACTIVATING` / `FLATTENING` / `RECONCILING`.

### RMS bypass

`EMERGENCY_FLATTEN` intent mode:

- Check 3 (Strategy): PASS for CLOSE or EMERGENCY_FLATTEN
- Check 8 (Money per stock): PASS for CLOSE or EMERGENCY_FLATTEN
- Kill-switch path uses synthetic RMS PASS before basket — does not re-run full engine

Normal CLOSE signals from TradingView still go through full RMS evaluation.

## Submit pacing note

Kill-switch flatten orders go through the same `GatewayRateLimiter` on the **one** `IBKRExecutionAdapter` as ordinary orders. P0 (`EMERGENCY_FLATTEN`) uses the emergency reserve slice of the token bucket. Flatten also uses the same TWS socket; if that Gateway is down, flatten cannot fail over to another instance.

Target: per-gateway limiter with a reserved emergency slice — [`backend-multi-gateway.md`](backend-multi-gateway.md) (not built).

## IBKR leftover flatten (operator script)

App kill-switch flatten closes **Postgres OPEN** rows through OMS + `GatewayRateLimiter` on **client id 1**. Residual IBKR lines that are not in the ledger (orphans, outside-app fills) need a sidecar:

[`backend/scripts/oms/flatten_gateway_positions.py`](../backend/scripts/oms/flatten_gateway_positions.py)

- Opens a **second** `TWSClient` (default **client id 99**) so it does not disconnect `app.main`.
- Local `--pace 0.2` between `placeOrder`s (~5/sec). Does **not** share the in-process limiter.
- Default is **dry-run**. Nothing is submitted without `--apply`.
- Paper ports `{7497, 4002}` only unless `--allow-live`.
- Refuses `--client-id` equal to `IBKR_CLIENT_ID`.
- Logs: `storage/logs/{YYYY-MM-DD}/flatten-gateway.log`.

Arm kill-switch first so TradingView OPENs stay blocked. Prefer `POST .../square-off` for ledger positions. Use this script only for IBKR leftovers.

**Basket CRITICAL** is separate from kill-switch: when compensation fails, `BasketCoordinator` latches OPEN for `(account_id, strategy_id)` and `CriticalRecoveryService` auto-flattens leftover broker lines then calls `clear_critical` when a fresh snapshot is flat. Reconcile square-off (`POST /api/v1/reconcile/positions/flatten`) flattens IBKR only — it does **not** clear the CRITICAL latch. Monitor incidents on the Positions dashboard banner (`GET /api/v1/baskets/critical?ibkr_account=`).

Dry run, then apply (Gateway paper port **4002**; use **7497** for paper TWS):

```bash
cd /home/tradingapp/app/backend
.venv/bin/python scripts/oms/flatten_gateway_positions.py \
  --host 127.0.0.1 --port 4002 --client-id 99 \
  --account DUR919062 --sec-type CFD --pace 0.2

.venv/bin/python scripts/oms/flatten_gateway_positions.py \
  --host 127.0.0.1 --port 4002 --client-id 99 \
  --account DUR919062 --sec-type CFD --pace 0.2 --apply
```

Safe to run while `app.main` is up **if** client id stays 99 and `--pace` stays 0.2 (or slower). Do not point it at client id 1. Do not speed `--pace` toward 30 msg/sec.

## Events

Flatten emits via `BasketCoordinator._event`:

| Kind | When |
|------|------|
| `KILL_SWITCH_ACTIVATED` | Flatten worker started |
| `KILL_SWITCH_COMPLETED` | All positions flat |
| `KILL_SWITCH_UNRESOLVED` | Remaining exposure after flatten |

## Log greps

| Grep | Stage |
|------|--------|
| `EMERGENCY KILL SWITCH ACTIVATED` | New operation created |
| `KILL SWITCH REARMED FROM DB` | Startup cache hydrate (armed accounts) |
| `KILL SWITCH CLEARED` | Operator disarm |
| `KILL_SWITCH_ACTIVE: Blocking NEW open signal` | OPEN rejected at fan-out |
| `Kill Switch persisted position close` | Position closed after flatten fill |
| `Reconciled stale position to CLOSED during Kill Switch` | Tier-2 reconciliation |
| `Kill Switch operation_id=.* finalized` | Operation complete |

## Do not break (agent invariants)

1. DB write before cache clear — if clear fails, account stays blocked (safe direction).
2. Never treat `COMPLETE` as permission to OPEN — only `CLEARED` disarms.
3. Always call `hydrate_kill_switch_cache` on startup (via `hydrate_runtime_from_db`).
4. Flatten must use `EMERGENCY_FLATTEN` — do not size CLOSE from webhook payload.
5. Do not block CLOSE signals when account is armed.
6. Reconciliation must check order ledger before marking positions CLOSED.

## Related docs

- HTTP details: [`backend-api.md`](backend-api.md)
- RMS / basket: [`backend-rms-oms.md`](backend-rms-oms.md)
- Operator safety: [`safety.md`](safety.md)
- Repair script: `backend/scripts/repair_historical_killswitch_positions.py`
- IBKR leftover flatten: `backend/scripts/oms/flatten_gateway_positions.py` (this file, section above)
