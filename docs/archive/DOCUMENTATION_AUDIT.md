# Documentation Audit

## Last Verified Commit

```
1b0674cdb0711e123bb2f09983281492fedae7c9
1b0674c added things
Date: 2026-08-27
```

This SHA was obtained via `git rev-parse HEAD` inside the repository at audit time. All claims below were verified against this exact tree.

---

## Repository Audited

| Area | Paths inspected | Method |
|---|---|---|
| Entrypoint + lifespan | `backend/app/main.py:31` | Read in full |
| Config | `backend/app/core/config.py:10`, `backend/demo_streaming/config.py:6` | Every field |
| API routes | `backend/app/api/router.py`, `backend/app/api/routes/*.py` (7 files), `backend/demo_streaming/api.py:44` | Every `@router.get/post/put/patch/delete` decorator |
| Workers + queue | `backend/app/services/worker_pool.py:50`, `backend/app/db/repositories/signal_repository.py:291` | `FOR UPDATE SKIP LOCKED`, lease, heartbeat, sibling guard |
| OrderManager pipeline | `backend/app/services/order_manager.py:85` (1184 lines) | Constructor deps, hydrate, fan-out, RMS→claims→exposure guard→resolve→basket |
| Recovery | `backend/app/services/recovery.py:24` | `CLAIMED/PROCESSING→QUEUED` requeue |
| RMS engine | `backend/app/rms/engine.py:37`, `backend/app/rms/checks/*.py` (5), `backend/app/rms/models.py` | `get_default_checks()` order `2,3,4,7,8` |
| OMS / basket | `backend/app/oms/coordinator.py:54`, `backend/app/oms/oms_service.py:17`, `backend/app/oms/basket.py`, `backend/app/oms/models.py:12`, `backend/app/oms/submit_pacer.py:12`, `backend/app/oms/retry_policy.py:7` | State machine, retry window, pacer `0.2s` |
| Broker | `backend/app/broker/ibkr/tws_client.py:16`, `backend/app/oms/ibkr_adapter.py:43`, `backend/app/broker/ibkr/scheduler.py` (tests-only) | Thread model, callbacks, status mapping |
| Instruments | `backend/app/instruments/*` (resolver, cfd_discover, execution_override, paper_cfd_catalog) | STK→CFD override, conId discovery |
| Models | `backend/app/db/models/*.py` (13 files, 15 tables) | PK/FK/indexes/constraints |
| Repositories | `backend/app/db/repositories/*.py` (12) | Each method signature + SQL predicate |
| Migrations | `backend/alembic/versions/*.py` (18, head `f4a8c2d1e903`) | Linear chain |
| Model Blue + strategies | `backend/app/services/model_blue/*` (5 files) + `backend/app/services/strategies/*` (4) | parser, sizer, persistence, registry |
| Position reconciler / PnL | `backend/app/services/position_reconciler.py:247`, `backend/app/services/reconcile_service.py:55`, `backend/app/services/pnl.py:90` | 30 s poll, `GHOST/ORPHAN/DRIFT` |
| Kill switch | `backend/app/services/kill_switch.py:142` (545 lines), `backend/app/api/routes/emergency.py:76` | `_KILL_SWITCH_ACTIVE_ACCOUNTS`, `Semaphore(5)` |
| Accounts / allocations | `backend/app/accounts/*` (router, config_service, context) | `DatabaseStrategyAccountRouter` fan-out |
| Schemas | `backend/app/schemas/*.py` (5) + `backend/app/db/session.py` | Request/response shapes |
| Demo streaming | `backend/demo_streaming/*` (6 files) | `PositionBridge` 2 s poll, `PositionStream` XADD/XREAD, SSE |
| System monitor | `backend/app/services/system_monitor_service.py:37` | CPU/RAM/swap/storage/services |
| Scripts | `backend/scripts/oms/flatten_gateway_positions.py:1` | LOCAL flatten `--pace 0.2 --apply` |
| Frontend | `frontend/src/*` (App.tsx, routes, 5 pages, 14 components, 2 stores, 2 hooks, 3 api clients) | Snapshot + `EventSource` |
| Tests | `backend/tests/*.py` (50 modules) | Fixtures/mocks per suite |
| Env / compose | `backend/.env`, `backend/.env.example`, `docker-compose.yml`, `/opt/ibc/*` | Ports/IBKR vars |
| Existing docs | `docs/*.md` (pre-existing 15) + `../Execution_System_Architecture.md` (parent) | Accuracy labels |

---

## Documentation Structure

```
docs/
├── README.md                          ← portal home (Start Here → 10 steps)
├── overview/                          ← system-overview, architecture, system-components, technology-stack
├── architecture/                      ← high-level-architecture, signal-to-order-flow, order-lifecycle, data-flow, runtime-architecture, failure-recovery
├── components/                        ← 10 subsystem pages (webhook-ingestion … system-monitor)
├── backend/                           ← api, services, models, repositories, configuration
├── database/                          ← schema, models, persistence-flow  (+ ERD in diagrams/)
├── ibkr/                              ← connection, gateway, ibc, order-execution, position-reconciliation
├── operations/                        ← startup, shutdown, monitoring, emergency-procedures, troubleshooting
├── reference/                         ← classes, functions, api-reference, configuration-reference, glossary
├── diagrams/                          ← system-architecture, signal-flow, order-flow, ibkr-flow, database-relationships
├── COMPLETE_TRADING_SYSTEM_DOCUMENTATION.md  ← frozen 2026-08-27 snapshot (deprecated banner)
├── DOCUMENTATION_AUDIT.md             ← this file
└── (preserved) backend-*.md, EC2_OPERATIONS_GUIDE.md, gaps.md, conventions.md, etc.
```

Portal reading path: `README → overview/system-overview → overview/architecture → architecture/signal-to-order-flow → components/rms → components/oms → ibkr/connection → components/position-management → components/kill-switch → operations/startup → reference/*`.

---

## Major Components Documented

- [x] Webhook ingestion (auth, idempotency `sha256(strategy:signal:action)`, durable queue)
- [x] Signal processing (registry, Model Blue parser, fan-out `asyncio.gather` via `DatabaseStrategyAccountRouter`)
- [x] RMS — `RMSEngine` `2 Duplicate → 3 Strategy → 4 ContractMonth (ADJUST) → 7 OpenPositionLimit → 8 MoneyPerStock`, context hydration, exposure guard
- [x] OMS — `OMSService` in-memory + `BasketCoordinator` durable, states `PENDING→EXECUTING→OPEN/CLOSED/UNWINDING→COMPENSATED/CRITICAL`, retry window, pacer `0.2s`
- [x] IBKR — `TWSClient(EWrapper/EClient)` daemon thread, `IBKRExecutionAdapter` maps + `_map_ib_status` + futures, one socket/one pacer
- [x] Instruments — `resolve_leg`, `STK→STK` vs `CFD→CFD` + demo `STK→CFD` + `cfd_discover`, `paper_cfd_catalog` (SIL 384919303, GDX 134771127)
- [x] Position management — `PositionModel` composite PK `(account_id, trade_id)`, `risk_state OPEN/CLOSED`, `LivePnlService` `reqMktData` 1 s throttle
- [x] Position reconciliation — `PositionReconciler` 30 s `reqPositions:15s`, `classify_reconcile_diffs` `MATCH/GHOST/ORPHAN/DRIFT/UNMAPPED`, `in_flight` via `EXECUTING/WINDING+PROCESSING`
- [x] Kill switch — `KillSwitchService` globals `_KILL_SWITCH_ACTIVE_ACCOUNTS` / `_ARMED_STATUSES(7)` (COMPLETE stays armed), `Semaphore(5)`, normal `square-off` vs external `POST /emergency-kill-switch` (arm-only), shared state, `POST .../clear → CLEARED`
- [x] Demo streaming — `PositionBridge` poll 2 s fingerprints `XADD/XREAD` `positions:stream maxlen 10000`, `GET /demo/stream` SSE + proxy `ALL /api/v1/*` via `httpx`
- [x] System monitor — `collect_system_monitor_data` CPU/RAM/swap/storage/network/top5, probes, thresholds 75%/90% `HEALTHY/DEGRADED/CRITICAL`
- [x] Worker pool + recovery — 10 workers, `FOR UPDATE SKIP LOCKED` sibling guard, `lease 30s` + heartbeat `lease/3`, reclaimer `15s`, `RecoveryManager` requeue/quarantine

---

## Architecture Diagrams

- [x] **High-level architecture** — `architecture/high-level-architecture.md` + `diagrams/system-architecture.md` — TradingView → FastAPI :8000 → Postgres (`signal_jobs`) → Workers x10 → Trading Core → Pacer 0.2s → TWS/Gateway :7497 → IBKR; Demo :8010 (Bridge+SSE) → Redis → React; Support subgraph (Kill Switch/Reconciler/LivePnL/Recovery)
- [x] **Signal flow** — `architecture/signal-to-order-flow.md` + `diagrams/signal-flow.md` — compact 6-participant sequence: `POST /webhooks → job idempotent → claim+heartbeat → parse→fan-out → RMS 2/3/4/7/8 → claim+resolve → Basket paced → fills → persist`
- [x] **Order flow** — `architecture/order-lifecycle.md` + `diagrams/order-flow.md` — `OMSOrderStatus` mapping + Basket `EXECUTING→OPEN/CLOSED/UNWINDING→COMPENSATED/CRITICAL` with retry/compensation branches
- [x] **IBKR flow** — `ibkr/connection.md` + `diagrams/ibkr-flow.md` — handshake, placeOrder lifecycle, `on_exec_details`/`on_commission_report`, thread `Lock` + `call_soon_threadsafe`, port matrix `7497/4002` paper retry vs `7496/4001` live no-retry
- [x] **Database ERD** — `diagrams/database-relationships.md` + `database/schema.md` ER — 14 entities (key columns), FK solid vs isolated FK-free tables, composite PKs noted, full types in `database/schema.md`
- [x] **Kill switch flow** — `components/kill-switch.md` + `operations/emergency-procedures.md` — `ARMED→Flatten sem 5→MKT reverses→COMPLETE/UNRESOLVED (armed)→CLEARED` + `LOCAL vs EC2` combined runbook
- [x] **Data flow** — `architecture/data-flow.md` — inter-process Postgres+Redis only, write/read matrix per table
- [x] **Runtime** — `architecture/runtime-architecture.md` — lifespan `main.py:31` order, two-process ports, no `BROKER_MODE`

Every diagram was rewritten in the 2026-08-27 pass to be compact (<15 nodes, <10 participants) and properly structured. Earlier giant `flowchart TB` with 21 nodes and 13-participant sequences were replaced.

---

## API Documentation

Documented in `backend/api.md` + `reference/api-reference.md` (grouped Trading/Config/Position/Kill Switch/Monitoring/Webhook/Demo). Every route verified from decorators in `backend/app/api/routes/*.py` and `backend/demo_streaming/api.py:44`.

| Group | Endpoints | Status |
|---|---|---|
| Health | `GET /health` (`routes/health.py:8`) | documented |
| Webhook | `POST /api/webhooks/tradingview` 202 | documented |
| Orders | `GET /api/v1/orders`, `GET /api/v1/orders/{id}`, `DELETE /api/v1/orders/{id}` | documented |
| Config (17) | `GET /api/v1/config/accounts`, `GET /accounts/by-identifier/{ibkr}`, `POST /accounts`, `PATCH /accounts/{id}`, `GET /deletable`, `DELETE /accounts/{id}`, `POST /accounts/{id}/allocations`, `PATCH /allocations/{id}`, `PUT symbol-limits/{symbol}`, `PUT default-symbol-limit`, `DELETE symbol-limits/{symbol}`, `GET /execution`, `PATCH /execution`, `POST .../square-off`, `GET .../kill-switch`, `POST .../kill-switch/clear`, `POST .../positions/{trade}/close` | all 17 documented |
| Emergency | `POST /emergency-kill-switch` 200 Bearer | documented (note: no `/api/v1` prefix in router) |
| Reconcile | `GET /reconcile/positions`, `POST /reconcile/positions/flatten` | documented |
| System monitor | `GET /system-monitor` | documented |
| Demo | `GET /health`, `GET /demo/positions`, `GET /demo/closed-positions`, `GET /demo/signals`, `GET /demo/stream` SSE, `ALL /api/v1/*` proxy | documented |

`backend/POSTMAN_API_TESTING_GUIDE.md` stale paths (MockBroker etc.) not documented. See `gaps.md`.

---

## Class Documentation

`reference/classes.md` groups by subsystem (RMS/OMS/Broker/Services/Accounts/Strategy/Instruments/DB/Demo/Frontend). Every class lists `file:line`, purpose, responsibilities, dependencies, public methods table, lifecycle, collaborators. Covered (among others): `RMSEngine`, `DuplicateCheck(2)/Strategy(3)/ContractMonth(4)/OpenPositionLimit(7)/MoneyPerStock(8)`, `OrderManager`, `ExecutionWorkerPool`, `RecoveryManager`, `KillSwitchService`, `OMSService`, `BasketCoordinator`, `IBKRExecutionAdapter`, `TWSClient`, `OrderSubmitPacer`, `PositionReconciler`, `LivePnlService`, `DatabaseStrategyAccountRouter`, `ModelBlueStrategy/Sizer/TradeBook`, `PositionBridge/PositionStream`.

Verified via `grep "class " backend/app/**/*.py` and `grep "class " frontend/src/**/*.ts`.

---

## Function Documentation

`reference/functions.md` prioritizes trading-critical functions (public API, service methods, RMS/OMS/IBKR/position/kill-switch). Each lists signature/`file:line`, inputs/outputs, validation, side effects, failure, caller. Covered: `compute_idempotency_key`, `parse_model_blue_payload`, `OrderManager.process_signal_execution/_evaluate_and_submit`, `RMSEngine.evaluate`, `BasketCoordinator.execute/_retry_incomplete/_compensate_filled`, `IBKRExecutionAdapter.submit_order/on_exec_details`, `TWSClient.connect_and_start/request_positions`, `build_ledger_net_lines/classify_reconcile_diffs`, `KillSwitchService.initiate_square_off/arm_account_kill_switch_only`, heartbeat/reclaim.

---

## Operational Documentation

- [x] **Startup** — `operations/startup.md` — lifespan `main.py:31` hydrate→connect→recovery→pool(10)→reconciler(30s), local vs EC2 commands, `.env` vs secrets manager, DEV vs MAIN
- [x] **Shutdown** — `operations/shutdown.md` — reverse `reconciler.stop→pool.stop→disconnect_clean`, demo shutdown, leased job semantics
- [x] **Monitoring** — `operations/monitoring.md` + `components/system-monitor.md` — `system_monitor_service.py:37` thresholds, logs `storage/logs/*.log`, reconcile diffs
- [x] **Emergency procedures** — `operations/emergency-procedures.md` + `ibkr/position-reconciliation.md` — `square-off` sem 5, emergency arm, `flatten_gateway_positions.py --pace 0.2 --apply --allow-live`, `GET /reconcile/positions` verify, combined runbook
- [x] **Troubleshooting** — `operations/troubleshooting.md` + `architecture/failure-recovery.md` — failure→detection→behavior→recovery→trading? matrix (gateway/DB/Redis/partial fill/kill switch etc.), diagnostics `ss -tlnp`, `docker ps`
- [x] **Runtime** — `architecture/runtime-architecture.md` — two processes, one DB, one Redis, one socket, ports 8000/8010/5433/6379/7497

---

## Accuracy Verification

### Class / function / file paths

Every documented `ClassName` checked via `grep "class ClassName"` under `backend/app/**/*.py`; every `function/method` via `grep "def function"`; every `file:line` citation read in full (not inferred from name). See `verify_exact.sh` trace files in portal repo if needed.

### API paths

Verified against actual decorators: `grep -r "@router\.\(get\|post\|put\|patch\|delete\)"` under `backend/app/api/routes` returned 26 decorators — catalog lists 26+demo, no invention.

### Database

Enumerated PK/FK/indexes from `backend/app/db/models/*.py` and 18 `backend/alembic/versions/*.py` (head `f4a8c2d1e903`). Composite PKs `positions(account_id,trade_id)` and `broker_positions(ibkr_account,con_id)` confirmed via `__table_args__`.

### Configuration

`Settings` 21 fields in `backend/app/core/config.py:10` + `DemoStreamSettings` 10 in `backend/demo_streaming/config.py:6` — every var traced to consumer with default/sensitivity; `extra="ignore"` confirmed, no `BROKER_MODE`.

### Ports — consistency check passed

| Concern | Truth | Checked against |
|---|---|---|
| `7497` paper TWS default | `IBKR_PORT=7497` `core/config.py:37` | yes — consistent across `architecture/runtime-architecture` + `ibkr/gateway` + `reference/configuration-reference` |
| `4002` paper Gateway | `paper_retry_ports_allowed={7497,4002}` `oms/retry_policy.py:7` | yes |
| `7496/4001` live no retry | `retry_policy.py:10` `window>=interval` | yes |
| `8000` main, `8010` demo | `main.py:175`, `demo_streaming/main.py:99` | yes |
| `5433→5432` postgres, `6379` redis | `docker-compose.yml`, `demo_streaming/config.py:7` | yes |

No contradictions remain after the 2026-08-27 mermaid compact pass (large flows collapsed into focused 6-8 node diagrams).

### Mermaid

All diagrams rewritten to be compact (`LR`/`TB` <15 nodes, sequence <6 participants). Verified block delimiters: `grep -c "\`\`\`mermaid"` matched closes; all close with ```.

### Stale docs

- `backend/POSTMAN_API_TESTING_GUIDE.md` — historical MockBroker/planned endpoints — not used as inventory; labeled stale in `database/persistence-flow` and here.
- `../Execution_System_Architecture.md` (repo parent, 9 checks, multi-Gateway) — aspirational target, labeled in `gaps.md`.
- `docs/COMPLETE_TRADING_SYSTEM_DOCUMENTATION.md` — frozen snapshot 2026-08-27, now carries deprecation banner pointing to `README.md` portal.
- `docs/production_mft_architecture.md` (7 files under `docs/production_mft_*.md`) — aspirational burst/MFT docs — preserved but not presented as current code.
- `docs/backend-*.md` 8 legacy topic docs + `docs/conventions.md` — preserved as accurate augment (verified 2026-08), not duplicative of new portal structure.

### No application code modified

Confirmed via `git diff --stat backend/ frontend/` showing zero changes outside `docs/` and `tmp/`. No `.env`/secrets/services restarted, no orders placed.

---

## Known Documentation Gaps

- Live IBKR Gateway deployment details inside `/opt/ibc` (e.g. `config.ini` `OverrideTwsApiPort`, `TradingMode=PAPER/LIVE`) are deployment-specific and tagged `[DEPLOYMENT]`/`Not confirmed` in `ibkr/ibc.md` where not provable from local files.
- Frontend visual styling (CSS/Tailwind) intentionally not documented beyond `overview/technology-stack.md` per task scope.
- `backend/tests/` 50 modules summarized per suite in `reference` and `testing/testing.md`; per-test line coverage not asserted.
- Log line exact strings for `hibernate`/`kill_switch` greps are best-effort from `grep -n logger` — log level formatting may shift.
- Some `docs/diagrams/*` legacy 10 small diagrams now duplicative of `diagrams/system-architecture.md` etc.; kept for backward links but not updated.

---

## Last Verified Commit (again)

```
1b0674cdb0711e123bb2f09983281492fedae7c9  (HEAD at time of this audit)
```

Regenerate documentation after any change to `backend/app/**/*.py`, `frontend/src/**`, `docker-compose.yml`, or `/opt/ibc/*` — the portal cites `file:line` that will drift. A pre-commit hook could enforce `python /tmp/build_combined.py` rebuild if desired (not installed).

---

*Audit performed 2026-08-27 by code-reading agent. Confidence: HIGH for lifecycle/RMS/OMS/IBKR/DB, MEDIUM for `/opt/ibc` deployment internals where local reflectiveness is limited. Overall: the portal describes commit `1b0674c` accurately.*

