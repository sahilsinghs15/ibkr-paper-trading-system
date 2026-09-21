# Appendix B — Error Index

Symptom → where to read. Use this when something is wrong and you do not yet
know which subsystem owns it.

## Log lines and exceptions

| Message | Meaning | Chapter |
|---|---|---|
| `RED_ZONE_BLOCKED signal ... deferred (projected red zone)` | Correct behaviour outside the trading window | [6](../part1_understanding/06_rms.md) |
| `AUTO_SQUARE_OFF_RETRY` | A leg is being retried for its remaining quantity | [7](../part1_understanding/07_oms_and_basket_execution.md) |
| `AUTO_SQUARE_OFF_RETRY_BLOCKED` | RMS rejected a retry | [7](../part1_understanding/07_oms_and_basket_execution.md) |
| `BASKET_UNWINDING` | Naked-pair protection is compensating | [7](../part1_understanding/07_oms_and_basket_execution.md) |
| `ROGUE_TRADE_DETECTED` / `_RESOLVED` | Broker/ledger mismatch confirmed or cleared | [14](../part2_safety_operations/14_recovery_and_reconciliation.md) |
| `Position reconcile run_id=... orphan=N drift=N ghost=N` | Sweep summary | [14](../part2_safety_operations/14_recovery_and_reconciliation.md) |
| `Worker ... LOST its lease on job ...` | Lease taken by another worker | [4](../part1_understanding/04_worker_execution.md) |
| `Worker ... failed to renew heartbeat ...; treating as lease_lost` | Heartbeat threw; failing closed (correct) | [4](../part1_understanding/04_worker_execution.md) |
| `Could not connect to TWS/Gateway` | Gateway unavailable; audited as `FAILED` | [8](../part1_understanding/08_ibkr_integration.md) |
| `audit_events is append-only: DELETE is not permitted` | Trigger refusing a delete (correct) | [12](../part2_safety_operations/12_operator_audit.md) |
| `audit_events row <uuid> is finalized and immutable` | Trigger refusing an update (correct) | [12](../part2_safety_operations/12_operator_audit.md) |
| `Audit trail unavailable; the operation was not executed.` (503) | `operation(required=True)` failed closed | [12](../part2_safety_operations/12_operator_audit.md) |
| `Concurrent idempotent replay (race) for manual order` | Unique-constraint loser replaying; no second broker order | [11](../part2_safety_operations/11_manual_trading.md) |
| `MANUAL_POSITION_CLOSED_TRADE_ID_REUSE_REJECTED` | Attempt to reuse a closed `trade_id` | [11](../part2_safety_operations/11_manual_trading.md) |
| `GatewayPacingTimeout` | Rate limiter exhausted; see finding C9 | [8](../part1_understanding/08_ibkr_integration.md) |
| `operator does not exist: bigint = character varying` | `account_id` / `ibkr_account` type confusion | [5](../part1_understanding/05_account_routing_and_strategy.md) |

## Test-environment errors

| Message | Cause | Chapter |
|---|---|---|
| `got Future ... attached to a different loop` | Leaked dependency override or loop-bound engine reuse | [16](../part3_engineering_history/16_reliability_fixes.md) |
| `cannot perform operation: another operation is in progress` | asyncpg connection used across loops | [16](../part3_engineering_history/16_reliability_fixes.md) |
| `Failed to stop <unit>: Access denied` | No systemd units on a dev machine — expected | [18](../part3_engineering_history/18_operational_fixes.md) |
| `Input should be 'ibgateway', 'backend', 'webhook' or 'watchdog'` | Service key is `backend`, not `trading-backend` | [12](../part2_safety_operations/12_operator_audit.md) |

## Operator-visible symptoms

| Symptom | Likely cause | Chapter |
|---|---|---|
| Positions still shown after a kill-switch flatten | 202 response; no resync trigger | [10](../part2_safety_operations/10_kill_switch.md) |
| Signal shows "LEG 1 / LEG 2 / LEG 3" for a pair | Retries rendered as legs | [7](../part1_understanding/07_oms_and_basket_execution.md) |
| Retry progress never appears | Frontend listened for an event kind never emitted | [7](../part1_understanding/07_oms_and_basket_execution.md) |
| P&L blank | `market_data_status` UNAVAILABLE — deliberate | [18](../part3_engineering_history/18_operational_fixes.md) |
| Manual orders blocked with no active flatten | Manual cache not released on UNRESOLVED (fixed) | [10](../part2_safety_operations/10_kill_switch.md) |
| Manual ticket blocked after flattening only the *signal* positions | Engine and account scope shared one cache (fixed) | [10](../part2_safety_operations/10_kill_switch.md) |
| "account-wide kill switch is active" with no operator flatten | Daily target/stop breach arms `account` scope automatically | [10](../part2_safety_operations/10_kill_switch.md) |
| Manual lot shows FLATTENED_PENDING_PRICE but is still open at IBKR | Broker/engine quantity compared across two instants (fixed) | [10](../part2_safety_operations/10_kill_switch.md) |
| `broker evidence not trusted for trade_id=... — retrying` | Correct: reconciliation declined an ambiguous comparison | [10](../part2_safety_operations/10_kill_switch.md) |
| Reject reason shows a number instead of `DU…` | `accounts.id` type comparison (fixed) | [5](../part1_understanding/05_account_routing_and_strategy.md) |
| Position quantity and filled quantity disagree | Execution mis-attribution, 2026-09-15 | [17](../part3_engineering_history/17_data_integrity_fixes.md) |

## Migration errors

| Symptom | Cause | Chapter |
|---|---|---|
| `alembic upgrade head` does nothing; tables missing | Mergepoint with only one parent stamped | [2](../part1_understanding/02_database_and_persistence.md) |
