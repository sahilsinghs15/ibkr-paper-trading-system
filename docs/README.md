# Docs index

**Verified from:** live routers under `backend/app/api/`, `backend/demo_streaming/api.py`, and the files linked below.

Agent entrypoint: [`../AGENTS.md`](../AGENTS.md).

## Not current (do not copy as inventory)

| Document | Role |
|----------|------|
| `Execution_System_Architecture.md` (parent of repo, not in git tree) | **Target** architecture (multi-process OMS, nine RMS checks, etc.). Not a description of this FastAPI app. |
| [`../backend/POSTMAN_API_TESTING_GUIDE.md`](../backend/POSTMAN_API_TESTING_GUIDE.md) | Historical Postman notes; documents MockBroker / place-order / positions / margin routes that **do not** exist in code. |
| [`../backend/docs/DEVELOPER_EXECUTION_GUIDE.md`](../backend/docs/DEVELOPER_EXECUTION_GUIDE.md) | Older human guide; stale. Prefer this `app/docs/` tree. |

## Which file to open

| If you need… | Open |
|--------------|------|
| Debug webhook → fill path / log greps | [`backend-execution.md`](backend-execution.md) |
| Package tree / where to change code | [`backend-map.md`](backend-map.md) |
| Jobs, workers, leases, claims, recovery | [`backend-concurrency.md`](backend-concurrency.md) |
| Kill switch / emergency flatten / IBKR leftover flatten script | [`backend-kill-switch.md`](backend-kill-switch.md) |
| Exact HTTP methods and paths | [`backend-api.md`](backend-api.md) |
| Every `Settings` / demo env field | [`backend-config.md`](backend-config.md) |
| Postgres tables, Alembic, Redis scope | [`backend-persistence.md`](backend-persistence.md) |
| RMS check list, basket states, TWS | [`backend-rms-oms.md`](backend-rms-oms.md) |
| Multi-account / multi-gateway / rate limits (as-is vs target) | [`backend-multi-gateway.md`](backend-multi-gateway.md) |
| How to run tests | [`backend-testing.md`](backend-testing.md) |
| Vite scaffold + demo HTML UI | [`frontend.md`](frontend.md) |
| Adding routes / DI conventions | [`conventions.md`](conventions.md) |
| Paper ports, STK→CFD override | [`safety.md`](safety.md) |
| Explicit not-implemented list | [`gaps.md`](gaps.md) |
| EC2 paper host snapshot (ops) | [`EC2_OPERATIONS_GUIDE.md`](EC2_OPERATIONS_GUIDE.md) |
| Watchdog (monitoring, Telegram, recovery) | [`watchdog.md`](watchdog.md) |
| Doc inventory (accuracy) | this file, next section |

## Doc inventory (accuracy vs code)

Produced by reading the files in this tree plus `AGENTS.md` / READMEs, then tracing `webhooks.py` → `worker_pool.py` → `order_manager.py` → `ibkr_adapter.py` → `tws_client.py`. Labels:

- **ACCURATE** — matches current code
- **STALE** — was true or host-true; code or product moved
- **ASPIRATIONAL** — design / target, not implemented (keep, but labeled)
- **UNDOCUMENTED** (before this pass) — in code, missing from docs; now covered in the named file

| Path | Covers | Verdict |
|------|--------|---------|
| [`README.md`](README.md) (this file) | Index | ACCURATE; inventory + multi-gateway link added |
| [`backend-execution.md`](backend-execution.md) | Webhook → RMS → basket → IBKR; log greps | ACCURATE; as-is now states **one socket**, **one job / N-account fan-out**, **no reconnect** |
| [`backend-map.md`](backend-map.md) | Package tree, lifespan, `app.state` | ACCURATE; singular `client` / adapter called out |
| [`backend-concurrency.md`](backend-concurrency.md) | Jobs, leases, claims, recovery | ACCURATE; `account_scope` unused on ingest now documented |
| [`backend-kill-switch.md`](backend-kill-switch.md) | Flatten API, armed vs cleared, IBKR leftover flatten script | ACCURATE; shares the one limiter/socket; sidecar client id 99 |
| [`backend-api.md`](backend-api.md) | HTTP inventory | ACCURATE; account CRUD has no gateway fields |
| [`backend-config.md`](backend-config.md) | `Settings` / demo env | ACCURATE; single `IBKR_*` triple |
| [`backend-persistence.md`](backend-persistence.md) | Tables, repos, Redis scope | ACCURATE; `accounts` has no gateway columns |
| [`backend-rms-oms.md`](backend-rms-oms.md) | RMS, basket, adapter, pacer | ACCURATE; multi-account = tag on one socket, not N Gateways |
| [`backend-multi-gateway.md`](backend-multi-gateway.md) | As-is connectivity; target N Gateways + per-gateway limiter; gap/plan | **New** (justified: no prior file fit) |
| [`backend-testing.md`](backend-testing.md) | pytest inventory | ACCURATE; `test_pacer.py` tests discover `RatePacer`, not live submit pacing |
| [`conventions.md`](conventions.md) | DI, docs hygiene | ACCURATE; one TWS owner process |
| [`frontend.md`](frontend.md) | Vite + `:8010` | ACCURATE; Settings cannot bind gateways |
| [`safety.md`](safety.md) | Ports, webhook 202, pacing | ACCURATE; reconnect log is a lie |
| [`gaps.md`](gaps.md) | Not-implemented list | ACCURATE; multi-gateway / limiter gaps added (intent moved, not deleted) |
| [`EC2_OPERATIONS_GUIDE.md`](EC2_OPERATIONS_GUIDE.md) | One paper EC2 host | **Mixed.** Ops paths are a dated host snapshot. `BROKER_MODE`, “no DATABASE_URL”, YAML capital are **STALE vs current Settings** (see that file’s banner). |
| [`../AGENTS.md`](../AGENTS.md) | Agent map / invariants | ACCURATE |
| [`../README.md`](../README.md) | Human repo README | ACCURATE |
| [`../backend/AGENTS.md`](../backend/AGENTS.md) | Pointer | ACCURATE |
| [`../backend/README.md`](../backend/README.md) | Backend runbook | ACCURATE |
| [`../frontend/README.md`](../frontend/README.md) | Frontend runbook | ACCURATE after unused-deps correction |
| [`../backend/POSTMAN_API_TESTING_GUIDE.md`](../backend/POSTMAN_API_TESTING_GUIDE.md) | Historical API | **STALE** (already labeled) |
| [`../backend/docs/DEVELOPER_EXECUTION_GUIDE.md`](../backend/docs/DEVELOPER_EXECUTION_GUIDE.md) | Historical map | **STALE** (already labeled). “Account DB not implemented” is false. |
| `Execution_System_Architecture.md` (parent dir) | Nine-check / multi-process OMS | **ASPIRATIONAL** — not in this git tree |

## Changelog (2026-08-28 — decoupled ingest + process manager)

| File | What changed | Why |
|------|--------------|-----|
| `README.md` (root) | **Rebuilt** as central portal with mermaid overview, 13-section nav | Replaces 30-line stub; now mirrors task §4 structure |
| `docs/README.md` | Fixed `Readme.md` → `README.md`, removed broken `../../Execution...` link, added this changelog row | Case bug + dead link |
| `backend-config.md` | Added `webhook_auth_*`, `emergency_killswitch_*`, `TRADINGAPP_TESTING` guard | Undocumented from `acdd451` |
| `backend-rms-oms.md` | Added managedAccounts gate (`UNMANAGED_ACCOUNT`) | Live in `tws_client.py:managedAccounts` + `ibkr_adapter.py:_validate` |
| `backend-execution.md` | Annotated `_validate_ibkr_account`, startup `recover_incomplete_baskets` defer, shutdown `critical_recovery.stop` | Recent PR |
| `backend-map.md` | Added `critical_recovery.stop`, managedAccounts row, Alembic head `a1b2c3d4e567` | Recent PR |
| `AGENTS.md` | Added `CriticalRecoveryService` in lifespan, run commands for `process_manager.py` | `acdd451` |
| `docs/archive/` | **Created** — moved `COMPLETE_*`, `DOCUMENTATION_AUDIT.md`, `production_mft_*.md` (7) | Bloat cleanup |
| Deleted (untracked) | `architecture/`, `backend/`, `components/`, `database/`, `diagrams/`, `ibkr/`, `integrations/`, `operations/`, `overview/`, `reference/`, `safety/`, `testing/`, `trading/`, `api/`, `.obsidian/` | 74-file auto-generated subtree; orphan, duplicative |

## Changelog (2026-08-24)

| File | What changed | Why |
|------|--------------|-----|
| [`backend-multi-gateway.md`](backend-multi-gateway.md) | **Created** — as-is citations, target N Gateways + per-gateway limiter, schema, diagrams, gaps, phases, open questions | No existing file could hold this without breaking one-topic docs |
| [`README.md`](README.md) | Inventory table; link; this changelog | STEP 0 + navigation |
| [`backend-execution.md`](backend-execution.md) | Fan-out, `ib_order.account`, one socket, no reconnect | UNDOCUMENTED as-is |
| [`backend-map.md`](backend-map.md) | Singular IB session; pointer | Agents must not add a second unpaced client |
| [`backend-concurrency.md`](backend-concurrency.md) | `account_scope` NULL; one job fans out N accounts | Job ≠ account |
| [`backend-rms-oms.md`](backend-rms-oms.md) | Account tagging vs Gateway; pacer scope | Stop describing multi-account as multi-gateway |
| [`backend-config.md`](backend-config.md) | Single `IBKR_*`; no per-account socket | Settings truth |
| [`backend-persistence.md`](backend-persistence.md) | No gateway tables | Schema truth |
| [`backend-api.md`](backend-api.md) | Config has no gateway binding | API truth |
| [`backend-kill-switch.md`](backend-kill-switch.md) | Flatten on the one socket/pacer | Failure/pacing semantics |
| [`safety.md`](safety.md) | Reconnect claim is false; pacer is process-global | Safety |
| [`gaps.md`](gaps.md) | Multi-gateway / limiter / reconnect gaps; design intent pointed at target doc | Keep intent, label not-built |
| [`conventions.md`](conventions.md) | Pin one submit process; pointer | `--workers N` would break in-process limiter |
| [`frontend.md`](frontend.md) | No gateway UI | Settings scope |
| [`backend-testing.md`](backend-testing.md) | Which pacer tests which class | Avoid false “token bucket is live” |
| [`EC2_OPERATIONS_GUIDE.md`](EC2_OPERATIONS_GUIDE.md) | Stale-env banner | Host snapshot vs current code |
| [`../AGENTS.md`](../AGENTS.md), [`../README.md`](../README.md), [`../backend/AGENTS.md`](../backend/AGENTS.md) | Pointers | Discovery |
| [`../frontend/README.md`](../frontend/README.md) | react-router / react-query **are** used | STALE unused-deps list |
