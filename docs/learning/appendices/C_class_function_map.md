# Appendix C — Class / Function Map

Where responsibility lives. Paths relative to the repository root.

## Entry points

| Path | Role |
|---|---|
| `backend/app/main.py` | Trading backend (`:8001`). Constructs the single `TWSClient` and `GatewayRateLimiter` |
| `backend/app/webhook_ingest.py` | Webhook ingest (`:8000`). Postgres only |
| `backend/demo_streaming/` | SSE + snapshots (`:8010`) |
| `backend/scripts/watchdog_main.py` | Watchdog |

## Execution path

| Class | File | Key methods |
|---|---|---|
| `ExecutionWorkerPool` | `app/services/worker_pool.py` | `_run_worker()`, `_lease_heartbeat()`, `_execute_job()`, `_write_status()` |
| `OrderManager` | `app/services/order_manager.py` | `process_signal_execution()`, `_update_runtime_state()`, `_exposure_guard()` |
| `RMSEngine` | `app/rms/engine.py` | `evaluate()` |
| `BasketCoordinator` | `app/oms/coordinator.py` | `execute()`, `_retry_incomplete()`, `_compensate_filled()`, `_basket_complete()`, `_retry_intent()` |
| `OMSService` | `app/oms/oms_service.py` | `submit_intent()`, `submit_one_leg()` |
| `IBKRExecutionAdapter` | `app/oms/ibkr_adapter.py` | Order submission, callback routing |

## Broker

| Class | File | Notes |
|---|---|---|
| `TWSClient` | `app/broker/ibkr/tws_client.py` | The only socket. `TWSClientThread` reader thread |
| `GatewayRateLimiter` | `app/broker/ibkr/gateway_rate_limiter.py` | `acquire()`, `blocking_acquire()`, priority buckets |
| `InstrumentResolver` | `app/instruments/resolver.py` | CFD↔underlying, size increments |

## Safety

| Class | File | Key methods |
|---|---|---|
| `KillSwitchService` | `app/services/kill_switch.py` | `initiate_square_off()`, `arm_account_kill_switch_only()`, `_execute_manual_flatten_operation()`, `_reconcile_and_finalize_manual()`, `_update_operation_completion()` |
| `SessionClock` | `app/services/session_clock.py` | `in_red_zone()`, `projected_in_red_zone()`, `resolved_session_close()` |
| — | `app/services/red_zone_release.py` | Post-session release of deferred signals |
| — | `app/services/cancel_exposure.py` | `check_cancel_exposure()` |
| `RecoveryManager` | `app/services/recovery.py` | `run_startup_recovery()` |

Module-level caches in `kill_switch.py`: `_KILL_SWITCH_ACTIVE_ACCOUNTS`,
`_MANUAL_KILL_SWITCH_ACTIVE_ACCOUNTS`, `_MANUAL_FLATTEN_ACTIVE_STATUSES`,
`_ARMED_STATUSES`; scope constants in `app/db/models/kill_switch.py`
(`KILL_SWITCH_SCOPE_ENGINE` / `_MANUAL` / `_ACCOUNT`).

## Manual trading

| Class | File |
|---|---|
| `ManualTradingService` | `app/services/manual_trading.py` — `submit_order()` holds the idempotency barrier |
| `ManualExecutionListener` | `app/services/manual_callbacks.py` — sole owner of manual fills; `_pending_commissions` |
| `ManualOrderRepository`, `ManualPositionRepository` | `app/db/repositories/manual_repository.py` |

## Positions, PnL, reconciliation

| Class | File |
|---|---|
| `LivePnlService` | `app/services/pnl.py` — sole owner of marks |
| `PositionRepository` | `app/db/repositories/position_repository.py` |
| `PositionReconciler` | `app/services/position_reconciler.py` — `run_once()`, rogue detection |
| — | `app/services/reconcile_service.py` — `classify_reconcile_diffs()`, `collect_reconcile_positions()` |

## Audit

| Component | File |
|---|---|
| Taxonomy | `app/audit/taxonomy.py` — `AuditCategory`, `AuditAction`, `AuditResult`, `ActorType`, `ACTION_CATALOG`, `INVENTORY_FIX_ACTIONS` |
| Recorder | `app/audit/recorder.py` — `record()`, `record_committed()`, `operation()`, `AuditEntry`, `AuditUnavailableError` |
| Context | `app/audit/context.py` — `AuditActor`, `RequestContext`, `build_request_context()` |
| Repository | `app/db/repositories/audit_repository.py` — `search()`, `AuditSearchFilters`, `parse_ip_filter()` |
| API | `app/api/routes/audit.py` |

## Notifications

| Component | File |
|---|---|
| Orchestrator + channels | `app/services/notification/` |
| Intelligence (flapping, suppression) | `app/services/notification/intelligence.py` |
| Canonical messages | `app/services/notification_canonical.py` — contains deprecated `send_canonical_telegram` |

## Frontend

| Component | File |
|---|---|
| `signalLegs` | `frontend/src/utils/signalLegs.ts` — `groupLogicalLegs()`, `latestRetryAttempt()`, `formatRetryLabel()` |
| `usePnlStream` | `frontend/src/hooks/usePnlStream.ts` — `resyncPositions()`, `scheduleFlattenResync()` |
| `pnlStore` | `frontend/src/store/pnlStore.ts` — `apply()`, `groupLegs()` |
| `signalStore` | `frontend/src/store/signalStore.ts` |
| Signal tray / detail | `frontend/src/components/SignalTrayTable.tsx`, `SignalDetailModal.tsx` |
| Kill switch | `frontend/src/components/KillSwitchModal.tsx`, `pages/AccountSettingsPage.tsx` |
| Audit UI | `frontend/src/components/audit/` |

## Backend-side mirrors of frontend logic

| Concern | Backend | Frontend |
|---|---|---|
| Logical leg grouping | `demo_streaming/snapshot.py` → `reconcile_signal_status()` | `utils/signalLegs.ts` → `groupLogicalLegs()` |

These two must agree. The frontend mirror was added 2026-09-21 after the
divergence described in [Chapter 7](../part1_understanding/07_oms_and_basket_execution.md).
