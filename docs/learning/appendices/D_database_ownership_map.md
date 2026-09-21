# Appendix D — Database Ownership Map

Which component writes which table. The authoritative reference is
[`docs/engineering/03-DATA-OWNERSHIP.md`](../../engineering/03-DATA-OWNERSHIP.md);
this appendix records ownership alongside the history that shaped it.

**Rule:** every table has exactly one authoritative writer. A change that writes
a table from a second place is wrong (`AGENTS.md` §4).

## Inbound and execution

| Table | Sole writer | Notes |
|---|---|---|
| `signal_jobs` | Ingest (insert) / `ExecutionWorkerPool` (claim, status) | Durability boundary. `FOR UPDATE SKIP LOCKED` |
| `signals` | `SignalRepository` | Parsed business-level signal |
| `execution_claims` | `ExecutionClaimRepository` | Dedup barrier; committed before broker submit |
| `baskets` | `BasketRepository` | Multi-leg state |
| `orders` | `OrderRepository` (engine path) | **One row per submission** — retries add rows on the same `leg` |
| `executions` | Execution persistence from `execDetails` | `exec_id` processed exactly once |
| `trade_executions` | Trade book sync | Idempotent upsert |

## Positions

| Table | Sole writer | Grain |
|---|---|---|
| `positions` | `OrderManager._update_runtime_state` → `PositionRepository` | Engine **pair** (leg A + B in one row) |
| `manual_positions` | `ManualExecutionListener` → `ManualPositionRepository` | Manual **lot**, keyed by `trade_id` |
| `broker_positions` | `PositionReconciler` | Snapshot of IBKR `position()` |
| `position_reconcile_runs` | `PositionReconciler` | Sweep records |

Kept separate by ADR 5; unified only at read time in `PositionReconciler`.

## Manual path

| Table | Sole writer | Notes |
|---|---|---|
| `manual_orders` | `ManualTradingService` | `uq_manual_orders_account_idempotency` on `(account_id, idempotency_key)` |
| `manual_executions` | `ManualExecutionListener` | |
| `manual_audit_events` | Manual path | Includes `MANUAL_POSITION_CLOSED_TRADE_ID_REUSE_REJECTED` |
| `manual_halt_state` | `KillSwitchService` | Manual submission block |

## Risk and safety

| Table | Sole writer | Notes |
|---|---|---|
| `kill_switch_operations` | `KillSwitchService` | `uq_kill_switch_operations_armed_account_scope` partial unique on armed statuses |
| `kill_switch_flatten_snapshots` | `KillSwitchService` | Immutable snapshot at arming time |
| `accounts` | `ConfigService` | Includes `trading_paused`, `cancel_exposure` |
| `account_loss_state` | Risk exit monitor | Realized-loss threshold crossing |
| `per_symbol_limits` | `ConfigService` | Per account, not global |
| `margin_settings`, `margin_rates` | `ConfigService` / margin cache | Singleton policy |
| `execution_settings` | `ConfigService` | Includes operator-configurable `max_retries` |
| `allocations`, `strategies` | `ConfigService` | |
| `instruments` | Instrument master | |

## Audit and observability

| Table | Sole writer | Notes |
|---|---|---|
| `audit_events` | `app/audit/recorder.py` **only** | **Append-only at the database level** via `audit_events_guard()` |
| `auth_sessions` | Auth path | Server-side sessions |
| `event_log` | Various, via event repository | Machine events. **Not** the operator audit trail |
| `notification_log`, `notification_deliveries` | Notification orchestrator | |
| `user_notification_state`, `user_notification_reads` | Notification read state | |
| `instance_daily_credit_usage` | System monitor credit ledger | |
| `users` | Auth / admin | |

## Do not delete from

`AGENTS.md` §8 forbids deleting historical rows from `orders`, `executions`,
`positions`, `trade_executions`, `event_log`, `manual_audit_events`,
`audit_events`, `auth_sessions` — to satisfy tests or to clear a UI error.

`audit_events` enforces this itself; the others rely on discipline.

## Constraints that carry safety weight

| Constraint | Table | Prevents |
|---|---|---|
| `uq_manual_orders_account_idempotency` | `manual_orders` | Duplicate manual broker orders |
| `uq_kill_switch_operations_armed_account_scope` | `kill_switch_operations` | Two concurrent flattens in one scope (finding C1) |
| `audit_events_guard()` trigger | `audit_events` | Tampering with or deleting audit history |
| CHECK on audit category | `audit_events` | Unknown categories |

These are load-bearing. Chapter 23 §1 explains why they live in the database
rather than in application code.
