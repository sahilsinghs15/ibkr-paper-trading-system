# IBKR Paper Trading System — Complete Technical Documentation

> **Deprecated:** This single-file snapshot was built on 2026-08-27 and is frozen. Prefer the portal at `docs/README.md` (Start Here → System Overview → ...). It will not be updated. See `docs/DOCUMENTATION_AUDIT.md` for the current index.

> **Single-file edition.** This document is the concatenation of every file in `docs/` produced by the codebase audit on 2026-08-27.
> The repository itself is the source of truth. Every section cites its exact source files and line numbers.
> `file:line` references refer to `backend/` or `frontend/` relative to the repository root.
> Diagrams use Mermaid — render them with any Mermaid-compatible viewer (GitHub, VS Code, etc.).

> **How it was built:** `python /tmp/build_combined.py` concatenates the 30 source docs in reading order.
> The individual files remain in `docs/` — this file is a convenience copy.

---

## Table of Contents

- [README / Onboarding](#readme--onboarding)
- **PART I — ARCHITECTURE**
  - [1. System Architecture](#1-system-architecture)
  - [2. Component / Container Diagram](#2-component--container-diagram)
- **PART II — TRADING**
  - [3. Trading Flow — Signal Lifecycle](#3-trading-flow--signal-lifecycle)
  - [4. Risk Management System (RMS)](#4-risk-management-system-rms)
  - [5. Order Management System (OMS)](#5-order-management-system-oms)
  - [6. Position Reconciliation](#6-position-reconciliation)
- **PART III — INTEGRATIONS**
  - [7. IBKR / Broker Integration](#7-ibkr--broker-integration)
  - [8. Redis / Streaming (Demo SSE)](#8-redis--streaming-demo-sse)
- **PART IV — DATABASE**
  - [9. Database — Tables, Columns & Migrations](#9-database--tables-columns--migrations)
  - [10. ER Diagram](#10-er-diagram)
- **PART V — REFERENCE**
  - [11. API Reference](#11-api-reference)
  - [12. Class Reference](#12-class-reference)
  - [13. Function Reference](#13-function-reference)
- **PART VI — SAFETY**
  - [14. Kill Switch](#14-kill-switch)
  - [15. Emergency Kill Switch](#15-emergency-kill-switch)
- **PART VII — OPERATIONS**
  - [16. Configuration](#16-configuration)
  - [17. Runtime / Deployment](#17-runtime--deployment)
  - [18. Failure & Recovery](#18-failure--recovery)
- **PART VIII — TESTING**
  - [19. Testing](#19-testing)
- **PART IX — DIAGRAMS**
  - [20. Diagram — Overall System](#20-diagram--overall-system)
  - [21. Diagram — Normal Trading Signal](#21-diagram--normal-trading-signal)
  - [22. Diagram — Order Execution (Basket)](#22-diagram--order-execution-basket)
  - [23. Diagram — IBKR Connection Lifecycle](#23-diagram--ibkr-connection-lifecycle)
  - [24. Diagram — Position Reconciliation](#24-diagram--position-reconciliation)
  - [25. Diagram — Kill Switch](#25-diagram--kill-switch)
  - [26. Diagram — Emergency Kill Switch (Local vs EC2)](#26-diagram--emergency-kill-switch-local-vs-ec2)
  - [27. Diagram — Gateway Failure / Reconnect](#27-diagram--gateway-failure--reconnect)
  - [28. Diagram — Frontend → Backend → Streaming](#28-diagram--frontend--backend--streaming)
  - [29. Diagram — Account Configuration Flow](#29-diagram--account-configuration-flow)
- **PART X — AUDIT**
  - [30. Documentation Audit Report](#30-documentation-audit-report)

---


---

## README / Onboarding

> **Source file:** `docs/README.md`  —  original heading: *IBKR Paper Trading System — Documentation Hub*

> **For the new engineer:** Start here. This page tells you what the system is, where to start reading, and where to find every detail.

## What Is This System?

A **paper-trading execution platform** for Interactive Brokers. It ingests TradingView webhook alerts, routes them through account-aware risk checks (RMS), executes multi-leg baskets through a **single** IBKR TWS/Gateway socket (paced at 0.2s), persists every state transition in PostgreSQL, and exposes a live PnL dashboard via a separate demo-streaming process on port 8010.

Two processes are required to run the full platform:

| Process | Command | Port | Purpose |
|---|---|---|---|
| **Main trading API** | `uvicorn app.main:app --host 127.0.0.1 --port 8000` | 8000 | Webhook ingestion, workers, RMS, OMS, IBKR |
| **Demo streaming** | `python -m demo_streaming` | 8010 | Polls DB → Redis → SSE for the frontend |

Paper ports `7497` (TWS) and `4002` (Gateway) are the only ports where automatic retry/compensation is enabled. The frontend is `frontend/src` (Vite + React 19) and is served from `:8010`, not from the main app.

## Where Should I Start?

**Recommended reading order for a new engineer:**

| Order | Document | Why |
|---|---|---|
| 1 | [System Architecture](architecture/system-architecture.md) | Purpose, components, data stores, failure boundaries |
| 2 | [Component Diagram](architecture/component-diagram.md) | Containers, dependencies, who talks to whom |
| 3 | [Trading Flow](trading/trading-flow.md) | End-to-end signal lifecycle with sequence diagram |
| 4 | [Class Reference](reference/classes.md) | Every important class with callers/callees |
| 5 | [RMS](trading/rms.md) | 5 risk checks, decision flow |
| 6 | [OMS](trading/oms.md) | Basket state machine, retry/compensation/CRITICAL |
| 7 | [IBKR Integration](integrations/ibkr.md) | Adapter, TWS client, pacing, callbacks |
| 8 | [Database](database/database.md) + [ER Diagram](database/er-diagram.md) | Tables, migrations, relationships |
| 9 | [Kill Switch](safety/kill-switch.md) + [Emergency Kill Switch](safety/emergency-kill-switch.md) | Safety systems, account isolation |
| 10 | [Runtime / Deployment](operations/runtime.md) | Ports, processes, startup order |
| 11 | [Configuration](operations/configuration.md) | Every env var with defaults |
| 12 | [Failure Recovery](operations/failure-recovery.md) | What happens when each piece fails |

## Full Documentation Index

### Architecture

| Document | Covers |
|---|---|
| [System Architecture](architecture/system-architecture.md) | Purpose, responsibilities, stack, processes, stores, network/failure boundaries + Mermaid |
| [Component Diagram](architecture/component-diagram.md) | Containers, package dependencies, data-flow + Mermaid |

### Trading

| Document | Covers |
|---|---|
| [Trading Flow](trading/trading-flow.md) | Webhook → parse → fan-out → RMS → OMS → IBKR → persistence, with sequence diagram + failure paths |
| [RMS](trading/rms.md) | Checks 2/3/4/7/8, engine, context, rejection behavior + decision-flow Mermaid |
| [OMS](trading/oms.md) | Basket states, OMSService, retry/compensation/CRITICAL, fills + sequence Mermaid |
| [Position Reconciliation](trading/position-reconciliation.md) | Broker vs ledger snapshot, diff kinds, 30s loop + Mermaid |

### Reference

| Document | Covers |
|---|---|
| [API Reference](reference/api.md) | Every HTTP endpoint: method, path, auth, body, response, status, side effects |
| [Class Reference](reference/classes.md) | Every significant class: file, purpose, state, methods, callers/callees |
| [Function Reference](reference/functions.md) | Important functions: params, returns, side effects, exceptions |

### Database

| Document | Covers |
|---|---|
| [Database](database/database.md) | Every table, columns, PK/FK/indexes, lifecycle, 18 migrations, Alembic usage |
| [ER Diagram](database/er-diagram.md) | Mermaid ER diagram from actual FKs |

### Integrations

| Document | Covers |
|---|---|
| [IBKR / Broker](integrations/ibkr.md) | Adapter, TWS client, scheduler (tests-only), pacing, instruments + Mermaid |
| [Streaming (Redis / SSE)](integrations/streaming.md) | Redis stream, PositionBridge, SSE, demo API, frontend flow + Mermaid |

### Safety

| Document | Covers |
|---|---|
| [Kill Switch](safety/kill-switch.md) | API, service, cache vs DB, blocking, Start Again + Mermaid |
| [Emergency Kill Switch](safety/emergency-kill-switch.md) | EC2 webhook (Bearer) vs local flatten script, auth, timeout, exit codes + Mermaid |

### Operations

| Document | Covers |
|---|---|
| [Configuration](operations/configuration.md) | Every `Settings` + `DemoStreamSettings` env var |
| [Runtime / Deployment](operations/runtime.md) | EC2, processes, ports, startup order, dependencies |
| [Failure Recovery](operations/failure-recovery.md) | Per-scenario Failure→Detection→Behavior→Recovery→Trading state |

### Testing

| Document | Covers |
|---|---|
| [Testing](testing/testing.md) | Test structure, suites, fixtures, mocks, how to run |

### Diagrams

| Diagram | File |
|---|---|
| Overall system | [diagrams/overall-system.md](diagrams/overall-system.md) |
| Normal trading signal | [diagrams/trading-signal.md](diagrams/trading-signal.md) |
| Order execution (basket) | [diagrams/order-execution.md](diagrams/order-execution.md) |
| IBKR connection lifecycle | [diagrams/ibkr-connection.md](diagrams/ibkr-connection.md) |
| Position reconciliation | [diagrams/position-reconciliation-diagram.md](diagrams/position-reconciliation-diagram.md) |
| Kill switch | [diagrams/kill-switch.md](diagrams/kill-switch.md) |
| Emergency kill switch (local vs EC2) | [diagrams/emergency-kill-switch.md](diagrams/emergency-kill-switch.md) |
| Gateway failure / reconnect | [diagrams/gateway-failure.md](diagrams/gateway-failure.md) |
| Frontend → backend → streaming | [diagrams/frontend-streaming.md](diagrams/frontend-streaming.md) |
| Account configuration flow | [diagrams/account-config-flow.md](diagrams/account-config-flow.md) |

### Existing Documentation (Preserved, Code-Verified)

These files pre-existed and were verified against the current codebase. Where the code disagrees with them, the new docs above are authoritative and discrepancies are noted in `DOCUMENTATION_AUDIT.md`.

| Document | Role | Verdict |
|---|---|---|
| [backend-execution.md](backend-execution.md) | Webhook → RMS → basket → IBKR; log greps | ACCURATE |
| [backend-map.md](backend-map.md) | Package tree / where to change code | ACCURATE |
| [backend-concurrency.md](backend-concurrency.md) | Jobs, workers, leases, claims, recovery | ACCURATE |
| [backend-kill-switch.md](backend-kill-switch.md) | Kill switch / emergency flatten | ACCURATE |
| [backend-api.md](backend-api.md) | Exact HTTP endpoints | ACCURATE |
| [backend-config.md](backend-config.md) | Env / Settings fields | ACCURATE |
| [backend-persistence.md](backend-persistence.md) | Tables, repos, Redis scope | ACCURATE |
| [backend-rms-oms.md](backend-rms-oms.md) | RMS checks / basket / TWS adapter | ACCURATE |
| [backend-multi-gateway.md](backend-multi-gateway.md) | Multi-account vs multi-gateway / rate limits | ACCURATE |
| [backend-testing.md](backend-testing.md) | pytest / ruff | ACCURATE |
| [frontend.md](frontend.md) | React PnL dashboard + demo UI | ACCURATE |
| [conventions.md](conventions.md) | How to add a route / DI rules | ACCURATE |
| [safety.md](safety.md) | Paper vs live ports, STK→CFD | ACCURATE |
| [gaps.md](gaps.md) | What is **not** implemented | ACCURATE |
| [EC2_OPERATIONS_GUIDE.md](EC2_OPERATIONS_GUIDE.md) | EC2 host snapshot (deployment) | Mixed — see banner inside |

**Do not confuse with stale docs:**

| Path | Why not source of truth |
|---|---|
| `../Execution_System_Architecture.md` (repo parent) | Target design with 9 RMS checks / multi-process OMS; not current code |
| `backend/POSTMAN_API_TESTING_GUIDE.md` | Historical; documents endpoints that do not exist |
| `backend/docs/DEVELOPER_EXECUTION_GUIDE.md` | Stale human guide |

## Quick Start

```bash
# Main trading API (needs PostgreSQL on :5433 per default DATABASE_URL)
cd backend
uv sync --extra dev
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000

# Demo streaming (needs PostgreSQL + Redis)
.venv/bin/python -m demo_streaming
# open http://127.0.0.1:8010/

# Frontend (Vite proxies /demo→:8010, /api/v1→:8000)
cd ../frontend
npm install && npm run dev   # http://127.0.0.1:5173/
npm run build                 # dist served by :8010
```

Tests / lint:

```bash
cd backend
.venv/bin/pytest
.venv/bin/ruff check app/ tests/ scripts/
```

## Accuracy Guarantee

Code is the source of truth. Every new document cites its source files and line numbers. See [DOCUMENTATION_AUDIT.md](DOCUMENTATION_AUDIT.md) for the full verification report and known discrepancies.

## How to Navigate Code from Docs

Every class/function reference includes `file_path:line_number` so you can jump directly to the source. Package dependencies are shown in [Component Diagram](architecture/component-diagram.md).

---

# PART I — ARCHITECTURE


---

## 1. System Architecture

> **Source file:** `docs/architecture/system-architecture.md`  —  original heading: *System Architecture — IBKR Paper Trading System*

> Source of truth: `backend/app/main.py`, `backend/app/core/config.py`, `backend/demo_streaming/*`, `backend/app/db/models/*`, `backend/app/services/*`, `backend/app/oms/*`, `backend/app/rms/*`, `backend/app/broker/ibkr/*`.
> Every statement below was verified against the codebase. See `docs/DOCUMENTATION_AUDIT.md`.

## 1. System Purpose

The system is a **paper-trading execution platform** for Interactive Brokers (IBKR). It ingests TradingView webhook alerts, routes them through account-aware risk checks (RMS), executes multi-leg baskets through a single IBKR TWS/Gateway socket, persists every state transition in PostgreSQL, and exposes a read-only PnL dashboard via a separate demo-streaming process.

It is **not a live-money trading system by default**. Paper ports `7497` (TWS) and `4002` (Gateway) are the only ports where automatic retry/compensation is enabled. Live ports are not blocked but retries are disabled there.

## 2. System Responsibilities

| Responsibility | Owner |
|---|---|
| Authenticated webhook ingestion with durable queuing | `app.api.routes.webhooks` + `SignalJobRepository` |
| 10-worker concurrent execution with lease/heartbeat | `app.services.worker_pool.ExecutionWorkerPool` |
| Strategy → account fan-out routing | `app.accounts.router.DatabaseStrategyAccountRouter` |
| Risk validation (5 checks) | `app.rms.engine.RMSEngine` |
| Atomic multi-leg basket execution | `app.oms.coordinator.BasketCoordinator` |
| Single-socket IBKR submission with 0.2s pacing | `app.oms.submit_pacer.OrderSubmitPacer` + `app.broker.ibkr.tws_client.TWSClient` |
| Fill/commission persistence and position lifecycle | `app.services.model_blue.persistence.ModelBlueExecutionPersistence` + `BasketCoordinator` |
| Live PnL via market-data subscriptions | `app.services.pnl.LivePnlService` |
| Emergency flatten and kill-switch arming | `app.services.kill_switch.KillSwitchService` |
| Periodic broker-vs-ledger reconciliation | `app.services.position_reconciler.PositionReconciler` |
| Read-only SSE streaming to the frontend | `backend/demo_streaming/*` |

## 3. Major Components

```
TradingView / External webhook source
        │
        ▼
FastAPI main app  (app.main:app, port 8000)  ──►  PostgreSQL (asyncpg)
        │  ├─ POST /api/webhooks/tradingview  (202 + durable SignalJob)
        │  ├─ ExecutionWorkerPool (10 workers + reclaimer)
        │  ├─ OrderManager  (RMS → OMS → BasketCoordinator)
        │  ├─ KillSwitchService / PositionReconciler / LivePnlService
        │  └─ TWSClient ─► IBKRExecutionAdapter ─► IB Gateway / TWS
        │
        │  (polls DB, XADD to Redis)
        ▼
Demo streaming app  (demo_streaming.main, port 8010)  ──►  Redis  ──►  SSE /demo/stream
        │                                                          │
        └────────────── proxies /api/v1/* to :8000 ───────────────┘
                              │
                              ▼
                    Vite React frontend  (frontend/src, port 5173 dev / dist served by :8010)
```

Two **independent OS processes** are required for full operation:

* **Main trading API** — `uvicorn app.main:app --port 8000` (lifespan in `app/main.py:31`)
* **Demo streaming** — `python -m demo_streaming` (entry `demo_streaming/main.py:72`, port `8010`)

## 4. Technology Stack

| Layer | Technology | Evidence |
|---|---|---|
| Language | Python 3.12 | `backend/pyproject.toml:requires-python` |
| Web framework | FastAPI + Uvicorn | `backend/pyproject.toml`, `app/main.py:145` |
| DB | PostgreSQL + asyncpg + SQLAlchemy 2.x + Alembic | `backend/pyproject.toml`, `backend/alembic/*` |
| Cache/stream | Redis (demo only) | `backend/demo_streaming/*`, `backend/pyproject.toml: redis` |
| IBKR API | ibapi (EWrapper/EClient) | `backend/pyproject.toml: ibapi`, `app/broker/ibkr/tws_client.py` |
| Frontend | React 19 + Vite + Zustand + TanStack Query + lightweight-charts | `frontend/package.json`, `frontend/src/store/*` |
| Validation | Pydantic v2 + pydantic-settings | `backend/pyproject.toml`, `app/core/config.py:10` |

No `BROKER_MODE` / MockBroker exists in `Settings` (`app/core/config.py:18` uses `extra="ignore"`).

## 5. External Dependencies

| Dependency | Usage | Required? |
|---|---|---|
| PostgreSQL | All durable state | Yes — main app fails without it |
| IB Gateway / TWS | Order submission, fills, positions, market data | Yes for trading; app starts without it but logs warning |
| Redis | Demo SSE stream (`positions:stream`) | Only for demo streaming process; main app does not use Redis |
| TradingView | Webhook source | External caller; no outbound dependency |

## 6. Runtime Processes

### Main app lifespan (`app/main.py:31`)

```
TWSClient
  → IBKRExecutionAdapter (OrderSubmitPacer 0.2s)
    → OMSService
      → OrderManager  (hydrate_runtime_from_db)
        → TWS connect_and_start
          → RecoveryManager.run_startup_recovery
            → ExecutionWorkerPool(10).start()
              → PositionReconciler.start()
```

Shutdown reverses this: reconciler → worker pool → `TWSClient.disconnect_clean()`.

### Demo streaming (`demo_streaming/main.py:72`)

```
Redis ping → PositionBridge.restore_baseline → bridge.run_forever (poll DB every 2s)
                                      ↘
                               uvicorn serve :8010  (SSE + /demo/* + proxy /api/v1/*)
```

`PositionBridge.poll_once` diffs `PositionModel`/`SignalJobModel` rows and `XADD`s to `positions:stream` (`stream_maxlen=10000`).

## 7. Communication Between Components

| From → To | Mechanism |
|---|---|
| TradingView → FastAPI | HTTPS POST JSON → `POST /api/webhooks/tradingview` |
| FastAPI → PostgreSQL | asyncpg via `AsyncSessionLocal` (`app/db/session.py`) |
| Worker pool → OrderManager | Direct async call `order_manager.process_signal_execution(signal)` |
| OrderManager → RMS | `rms_engine.evaluate(intent, rms_context)` (in-memory) |
| OrderManager → OMS/BasketCoordinator | `baskets.execute(intent, rms_result)` or `oms.submit_intent` |
| OMS → IBKRExecutionAdapter | `adapter.submit_order(OMSOrder)` (async, paced) |
| Adapter → TWSClient → IB Gateway | `TWSClient.placeOrder` / `reqOpenOrders` / `reqExecutions` / `reqMktData` (threaded `EClient.run()`) |
| IB Gateway → Adapter | `EWrapper` callbacks (`on_order_status`, `on_exec_details`, `on_commission_report`, etc.) on TWS thread |
| PositionReconciler → TWSClient | `request_positions_async(timeout=15s)` every 30s |
| LivePnlService → TWSClient | `reqMktData` per leg + `on_tick_price` |
| Main app DB → Demo streaming | Polling `load_position_rows` + `load_signals` every `demo_poll_interval_ms=2000` |
| Demo streaming → Redis | `PositionStream.xadd` (XADD) |
| Demo streaming → Frontend | SSE `GET /demo/stream` (`data: json\n\n`), plus `GET /demo/positions` snapshot |
| Frontend → Main app (via demo proxy) | `httpx.AsyncClient` proxy ` /api/v1/{path}` → `trading_api_url` (default `http://127.0.0.1:8000`) |

All inter-process communication between the two backend processes is **through PostgreSQL and Redis**; there is no direct RPC.

## 8. Data Stores

| Store | Purpose | Durability |
|---|---|---|
| PostgreSQL | Signals, jobs, orders, executions, baskets, positions, events, claims, accounts, limits | Durable, Alembic-migrated (head `f4a8c2d1e903`) |
| Redis stream `positions:stream` | Ephemeral position/signal diffs for SSE | Ephemeral, `maxlen 10000`, not a source of truth |
| Disk `data/tradingview_webhooks/` | Raw webhook capture JSON per request | Best-effort, plus append-only `incoming_signals.csv` (temporary) |
| `storage/logs/trading-YYYY-MM-DD.log` | Main app logs (midnight rollover) | File, `app/core/logger.py:DatedTimedRotatingFileHandler` |
| `storage/logs/demo-YYYY-MM-DD.log` | Demo stream logs | File |

## 9. Network Boundaries

* Main app binds `127.0.0.1:8000` by default (no CORS/WebSocket on `app.main`).
* Demo streaming binds `127.0.0.1:8010` by default; set `DEMO_STREAM_HOST=0.0.0.0` to expose publicly (SG TCP 8010).
* Frontend `vite.config.ts` proxies `/demo → 127.0.0.1:8010` and `/api/v1 → 127.0.0.1:8000` in dev.
* TWS/Gateway socket is `127.0.0.1:7497` by default (`app/core/config.py:37`). No multi-gateway pool exists.
* No ngrok binding for `app.main`; use `:8010` for remote dashboard access.

## 10. Failure Boundaries

| Boundary | Isolation |
|---|---|
| Webhook ingestion vs execution | Ingestion returns 202 after DB commit; workers execute asynchronously. A worker crash does not lose the job (lease recovery). |
| One signal vs another signal | Per-job lease (`ACTIVE_LEASE_STATUSES`) + `FOR UPDATE SKIP LOCKED` claim + per-domain lock `(account_scope, strategy_id)` + per-exposure lock `(account_id, symbol)` |
| One account vs another | `DatabaseStrategyAccountRouter` fans out independently; kill switch arms per `account_id`; RMS limits are per `(account_id, symbol)` |
| One leg vs other legs (basket) | `BasketCoordinator` detects partial fills, cancels working legs, compensates filled legs, or marks `CRITICAL` (blocks future OPENs via `is_open_blocked`) |
| Demo streaming vs trading | Demo process never mutates trading state; Redis failure does not affect order execution |
| IBKR disconnect vs DB | `on_connection_closed` marks non-terminal orders `ERROR`; `PositionReconciler` skips when `client.is_connected()==False`; orders remain durable in DB for recovery |

## 11. High-Level Architecture Diagram

```mermaid
flowchart TB
    TV["TradingView"]
    API["FastAPI :8000\nIngest + Routes"]
    DB[("PostgreSQL\nsignal_jobs etc.")]
    POOL["Workers x10\n+ heartbeat"]
    CORE["Trading Core\nParser → Router → RMS → Basket"]
    ADAPTER["IBKR Stack\nPacer 0.2s → Adapter → TWS"]
    GW["IB Gateway :7497"]
    IBKR["Interactive Brokers"]
    DEMO["Demo :8010\nBridge + SSE"]
    REDIS[("Redis\nstream")]
    FE["React Frontend"]

    TV --> API --> DB --> POOL --> CORE --> ADAPTER --> GW --> IBKR
    CORE -. persist .-> DB
    API -. capture .-> DB
    DB -. poll 2s .-> DEMO --> REDIS --> FE
    FE -. proxy /api/v1 .-> API

    subgraph Support["Support (same process)"]
        direction LR
        KS["Kill Switch"]
        RC["Reconciler"]
        PNL["Live PnL"]
        REC["Recovery"]
    end
    Support -.-> CORE
    Support -.-> ADAPTER
```

## 12. What Is NOT Built

Per `docs/gaps.md` and AGENTS.md:

* Multi-Gateway pool / per-gateway rate limiter — not implemented; one socket, one `OrderSubmitPacer`.
* `IBKRExecutionScheduler` (`broker/ibkr/scheduler.py`) is tests-only; production uses `OrderSubmitPacer`.
* CORS / WebSocket / static files on `app.main` — not implemented.
* `BROKER_MODE` / MockBroker — not implemented.

## 13. Source Files

* Entrypoint: `backend/app/main.py:31`
* Config: `backend/app/core/config.py:10`
* Routes: `backend/app/api/routes/{health,webhooks,orders,config,emergency,reconcile,system_monitor}.py`
* Queue/workers: `backend/app/services/worker_pool.py:50`
* Pipeline: `backend/app/services/order_manager.py:85`
* Recovery: `backend/app/services/recovery.py:24`
* RMS: `backend/app/rms/engine.py:37`
* OMS/basket: `backend/app/oms/{coordinator.py:48,oms_service.py:17,basket.py:23}`
* Broker: `backend/app/broker/ibkr/{tws_client.py:16,ibkr_adapter.py:43}`
* Demo: `backend/demo_streaming/{main.py:72,stream.py:8,publisher.py:90,api.py:44}`

---

## 2. Component / Container Diagram

> **Source file:** `docs/architecture/component-diagram.md`  —  original heading: *Component / Container Diagram*

> Verified against `backend/app/main.py`, `backend/demo_streaming/*`, `backend/app/db/models/*`, `backend/app/services/*`, `backend/app/oms/*`, `backend/app/rms/*`, `backend/app/broker/ibkr/*`, `frontend/src/*`.

## Container Overview

```mermaid
flowchart LR
    TV["TradingView"] --> API["API :8000"]
    API --> PG[("PostgreSQL")]
    PG --> POOL["Workers x10"]
    POOL --> OM["OrderManager"]
    OM --> RMS["RMS"]
    RMS --> BASKET["Basket + OMS"]
    BASKET --> ADAPTER["IBKR Adapter\n+ Pacer 0.2s"]
    ADAPTER --> TWS["TWSClient"]
    TWS <--> GW["Gateway :7497"]
    GW <--> IBKR["IBKR"]

    PG -.-> DEMO["Demo :8010"]
    DEMO --> REDIS[("Redis")]
    REDIS --> FE["Frontend"]

    subgraph Backend["Backend :8000"]
        direction TB
        API
        POOL
        OM
        RMS
        BASKET
        ADAPTER
        TWS
    end

    subgraph Support["Support"]
        direction TB
        KILL["Kill Switch"]
        RECON["Reconciler"]
        PNL["Live PnL"]
    end
    Support -.-> BASKET
    Support -.-> TWS
```

## Component Details

### FastAPI Main App (`backend/app/main.py:31`)

| Component | File | Responsibility |
|---|---|---|
| `health` router | `app/api/routes/health.py:8` | `GET /health → {"status":"ok"}` |
| `webhooks` router | `app/api/routes/webhooks.py:166` | `POST /webhooks/tradingview` → 202 + `SignalJobRepository.create_job_if_not_exists` + disk capture |
| `orders` router | `app/api/routes/orders.py:16` | `GET /orders`, `GET /orders/{id}`, `DELETE /orders/{id}` via `OMSService` |
| `config` router | `app/api/routes/config.py:43` | `GET/POST/PATCH /config/accounts`, allocations, symbol limits, execution settings, kill-switch clear/square-off |
| `emergency` router | `app/api/routes/emergency.py:76` | `POST /emergency-kill-switch` (Bearer auth → arm DB state only) |
| `reconcile` router | `app/api/routes/reconcile.py:18` | `GET /reconcile/positions`, `POST /reconcile/positions/flatten` |
| `system_monitor` router | `app/api/routes/system_monitor.py:12` | `GET /system-monitor` (psutil + service health) |
| `ExecutionWorkerPool` | `app/services/worker_pool.py:50` | 10 workers, `claim_next_jobs(lease 30s)`, heartbeat `lease_duration/3`, reclaimer every 15s |
| `OrderManager` | `app/services/order_manager.py:85` | Strategy routing → RMS → execution claims → `BasketCoordinator`/`OMSService` |
| `RMSEngine` | `app/rms/engine.py:37` | Sequential evaluation of 5 `BaseRMSCheck` implementations |
| `OMSService` | `app/oms/oms_service.py:17` | In-memory order map, `submit_intent`/`submit_one_leg`/`cancel_order`, dedupe set |
| `BasketCoordinator` | `app/oms/coordinator.py:48` | Atomic basket EXECUTING→OPEN/CLOSED/UNWINDING→COMPENSATED/CRITICAL, retry/compensation |
| `IBKRExecutionAdapter` | `app/oms/ibkr_adapter.py:43` | `submit_order`/`cancel_order`/`fetch_broker_order_snapshot`, EWrapper callbacks, commission/exec handling |
| `TWSClient` | `app/broker/ibkr/tws_client.py:16` | `EWrapper`+`EClient` threaded socket, `connect_and_start`, `request_contract_details`, `request_positions` |
| `OrderSubmitPacer` | `app/oms/submit_pacer.py:12` | `async acquire() → bool` enforces `min_interval_sec=0.2` (one pacer, one socket) |
| `RecoveryManager` | `app/services/recovery.py:24` | Startup scan: requeue `CLAIMED/PROCESSING` jobs with `emitted==0` → `QUEUED`, quarantine `RECOVERY_REQUIRED`, hydrate runtime |
| `KillSwitchService` | `app/services/kill_switch.py:142` | `initiate_square_off` (flatten) + `arm_account_kill_switch_only` (emergency), `Semaphore(5)` |
| `PositionReconciler` | `app/services/position_reconciler.py:90` | 30s `request_positions` → `replace_snapshot` → `classify_reconcile_diffs` → `insert_run` + `POSITION_RECONCILE` event |
| `LivePnlService` | `app/services/pnl.py:90` | `reqMktData` per open leg, `on_tick_price` → `update_live_pnl` (throttled 1s) |
| `ModelBlue*` | `app/services/model_blue/*` | `parse_model_blue_payload`, `ModelBlueSizer`, `DatabaseModelBlueTradeBook`, `ModelBlueExecutionPersistence` |

### Demo Streaming (`backend/demo_streaming/*`)

| Component | File | Responsibility |
|---|---|---|
| `PositionBridge` | `demo_streaming/publisher.py:90` | Polls DB every `demo_poll_interval_ms=2000`, diffs fingerprints, `XADD`s `SIGNAL_RECEIVED`/`POSITION_*` |
| `PositionStream` | `demo_streaming/stream.py:8` | Redis `XADD`/`XREAD` wrapper (`positions:stream`, `maxlen 10000`) |
| `snapshot` helpers | `demo_streaming/snapshot.py:1` | `load_position_rows`, `load_signals`, `position_leg_payloads`, `reconcile_signal_status`, fingerprints |
| `Demo API` | `demo_streaming/api.py:44` | `GET /health`, `GET /demo/positions`, `GET /demo/closed-positions`, `GET /demo/signals`, `GET /demo/stream` (SSE), proxy ` /api/v1/{path}` via `httpx` |
| Config | `demo_streaming/config.py:7` | `DemoStreamSettings` (`demo_stream_host=127.0.0.1`, `port=8010`, `poll_interval_ms=2000`, `redis_url`, `trading_api_url`) |

### Frontend (`frontend/src/*`)

| Component | File | Responsibility |
|---|---|---|
| `App` | `frontend/src/App.tsx:1` | `BrowserRouter` → `/accounts`, `/account/:ibkrAccount`, `/settings` (redirect), global `usePnlStream()` |
| `usePnlStream` | `frontend/src/hooks/usePnlStream.ts:1` | Snapshot `GET /demo/positions` + `EventSource /demo/stream`, drives `usePnlStore` + `useSignalStore` |
| `usePnlStore` | `frontend/src/store/pnlStore.ts:1` | Zustand `active`/`closed`/`streamState`/`displayTz`, `apply(row)` + `groupLegs` |
| `useSignalStore` | `frontend/src/store/signalStore.ts:1` | Zustand signal tray with `getCanonicalStatus`, pagination, sound notification |
| Pages | `frontend/src/pages/*` | `AccountsPage`, `PositionsPage` (signals/open/closed tabs), `AccountSettingsPage`, `SystemMonitorPage`, `ReconcilePage` |

### Data Stores

| Store | Tables / Keys | Access |
|---|---|---|
| PostgreSQL | `signals`, `signal_jobs`, `orders`, `executions`, `baskets`, `positions`, `event_log`, `execution_claims`, `accounts`, `strategies`, `allocations`, `per_symbol_limits`, `execution_settings`, `kill_switch_operations`, `broker_positions`, `position_reconcile_runs`, `instruments`, `alembic_version` | `AsyncSessionLocal` (asyncpg) |
| Redis | Stream `positions:stream`, pub/sub not used | `redis.asyncio.Redis.from_url` (demo only; main app has no Redis dependency) |
| Disk | `data/tradingview_webhooks/*.json`, `incoming_signals.csv`, `storage/logs/*.log` | `pathlib` |

## Dependencies Between Packages

```
app.main
  → app.core.config / app.core.logger
  → app.db.session (engine + AsyncSessionLocal)
  → app.broker.ibkr.tws_client
  → app.oms.ibkr_adapter  → app.oms.submit_pacer + app.instruments.resolver
  → app.oms.oms_service    → app.oms.models + app.instruments.resolver
  → app.oms.coordinator    → app.oms.basket + app.oms.retry_policy + app.rms.engine
  → app.services.order_manager
        → app.rms.* + app.accounts.router + app.instruments.cfd_discover
        → app.services.model_blue.* + app.services.strategies.*
        → app.db.repositories.* + app.oms.coordinator
  → app.api.routes.*       → app.services.* + app.accounts.config_service
  → app.services.worker_pool + app.services.recovery

demo_streaming
  → demo_streaming.config / demo_streaming.stream (redis) / demo_streaming.snapshot (DB reads) / demo_streaming.publisher

frontend
  → /demo/* + /demo/stream (demo streaming)
  → /api/v1/* (main API proxied through demo)
```

## Not Implemented (Confirmed Absent)

* Multi-Gateway pool / per-gateway limiter / reconnect-on-drop — not implemented (`docs/backend-multi-gateway.md`).
* `IBKRExecutionScheduler` (`app/broker/ibkr/scheduler.py`) is tests-only; production uses `OrderSubmitPacer`.
* CORS / WebSocket / static serving on `app.main` — not implemented.
* `BROKER_MODE` env / MockBroker — not implemented (`app/core/config.py:18` `extra="ignore"`).

---

# PART II — TRADING


---

## 3. Trading Flow — Signal Lifecycle

> **Source file:** `docs/trading/trading-flow.md`  —  original heading: *Trading Flow — Signal Lifecycle*

> Every step below traces actual call sites. File + function references are exact.

## Overview

A TradingView alert travels from HTTP ingestion through a durable queue, 10 concurrent workers, per-account fan-out, 5 RMS checks, execution claims, atomic basket execution over a single TWS socket, and finally to PostgreSQL persistence and SSE streaming.

## Step-by-Step Lifecycle

### 1. Incoming Webhook — HTTP 202 Accepted

| Item | Value |
|---|---|
| **Route** | `POST /api/webhooks/tradingview` |
| **File** | `backend/app/api/routes/webhooks.py:166` |
| **Function** | `receive_tradingview_webhook(request)` → `_process_tradingview_webhook(...)` |
| **Auth** | `X-Webhook-Secret` vs `Settings.webhook_auth_secret` via `hmac.compare_digest` (`webhooks.py:144`). Skip if `webhook_auth_enabled=false` or secret is `None`. |
| **Validation** | `json.loads(body_bytes)` → 400 if malformed or non-dict. |
| **Idempotency** | `compute_idempotency_key(payload)` (`app/services/worker_pool.py:27`): normalize `strategy_id`/`trade_id`, handle `:CLOSE` suffix, `sha256("{strategy}:{signal}:{action}")` → `(strategy_id, signal_id, trade_id, idempotency_key)` |
| **Durable write** | `SignalJobRepository.create_job_if_not_exists(idempotency_key, ...)` inside `session.begin()` (`webhooks.py:249`). Unique constraint on `idempotency_key` makes the insert idempotent; duplicate returns existing job with `created=False`. Also writes disk capture `data/tradingview_webhooks/*.json` and append-only `incoming_signals.csv` (temporary). |
| **Response** | `TradingViewWebhookResponse(status="accepted", source="tradingview", signal_id, job_id, request_id)` with HTTP 202. |
| **Failure** | Auth fail → 401, malformed JSON → 400, DB unavailable → 500, factory missing → 500. No trading side-effects on failure. |

### 2. Queue Claim — Workers Compete

| Item | Value |
|---|---|
| **Pool** | `ExecutionWorkerPool` (`app/services/worker_pool.py:50`), `worker_count=10`, started in `app/main.py:102` |
| **Claim** | `_claim_job(worker_id)` (`worker_pool.py:165`): `SignalJobRepository.claim_next_jobs(worker_id, limit=1, lease_duration_sec=30.0)` → `SELECT ... WHERE status IN (QUEUED,RECEIVED) FOR UPDATE SKIP LOCKED` with `trade_id` sibling guard, `UPDATE status=CLAIMED, worker_id, lease_expires_at=now+30s` |
| **Heartbeat** | `_lease_heartbeat(job_id, worker_id, cancel_event, lease_lost)` (`worker_pool.py:210`): every `max(2.0, lease_duration/3)` → `SignalJobRepository.heartbeat_lease(job_id, worker_id)` with fenced `WHERE worker_id=... AND status IN (CLAIMED,PROCESSING)`; sets `lease_lost` if 0 rows. |
| **Reclaimer** | `_reclaimer_loop` (`worker_pool.py:117`): every `reclaim_interval_sec=15.0` → `SignalJobRepository.reclaim_stale_jobs()` (lease-expired `CLAIMED/PROCESSING` → `QUEUED`, or `DEAD_LETTER` if `attempt_count>=max_attempts`) + `ExecutionClaimRepository.reconcile_stale_claims(300s)` |
| **Domain lock** | `_process_claimed_job` (`worker_pool.py:173`): `domain_lock = _get_domain_lock(account_scope, strategy_id)` — serializes jobs for the same `(account_scope, strategy_id)` slice. |

### 3. Parse — Strategy Resolution

| Item | Value |
|---|---|
| **Execute** | `_execute_job(worker_id, job, lease_lost)` (`worker_pool.py:277`): fenced `UPDATE status=PROCESSING`, then `order_manager.parse_inbound_payload(payload, timestamp, request_id, capture_data)` |
| **Parser** | `OrderManager.parse_inbound_payload` (`app/services/order_manager.py:339`) → `parse_tradingview_payload(payload, timestamp, request_id, capture_data, registry)` (`app/services/strategies/inbound.py:17`) |
| **Strategy routing** | `registry.get(strategy_id)` (`app/services/strategies/registry.py:8`): first handler whose `can_handle(strategy_id)` matches wins. Current handlers: `[ModelBlueStrategy]` (`app/main.py:140`). Fallback → `parse_legacy_signal` (`app/services/strategies/legacy.py:19`) |
| **Model Blue parse** | `parse_model_blue_payload` (`app/services/model_blue/parser.py:15`): validates `strategy=="model_blue"`, `action in {OPEN,CLOSE}`, `trade_id` required, `direction=±1`, `OPEN` requires 2 buckets each with `legs[0]` + `symbol` + `instrument_type` + `weight!=0` + `price>0` → `Signal(signal_type, strategy_id="model_blue", action, trade_id, direction, legs=(SignalLeg,SignalLeg))` |
| **Persist inbound** | `OrderManager._persist_inbound_signal(inbound_row, status=NEW)` (`order_manager.py:1004`) → `SignalRepository.record_inbound` + `EventRepository.append(SIGNAL_RECEIVED, SIGNAL_PERSISTED)` |
| **Failure** | Parse `ValueError` → `_write_status REJECTED` + `record_rejected_inbound` with `reject_reason`; heartbeat checks `lease_lost` before terminal write. |

### 4. Fan-Out — Strategy × Accounts

| Item | Value |
|---|---|
| **Dispatch** | `OrderManager._process_signal_execution_inner` (`order_manager.py:402`): `handler = registry.get(signal.strategy_id)`, then either `_process_legacy_single_name` (no router) or `account_router.resolve(strategy_id)` |
| **Account router** | `DatabaseStrategyAccountRouter.resolve(strategy_id)` (`app/accounts/router.py:50`): joins `AccountModel→AllocationModel→StrategyModel` where `enabled` and `alloc_pct>0`; computes `committed=total_margin*alloc_pct` per allocation; returns `list[AccountExecutionContext(account_id, ibkr_account, committed_notional, ...)]` |
| **Fan-out** | `_fanout_accounts(signal, handler, contexts, ...)` (`order_manager.py:494`): `asyncio.gather(*_fanout_single_account for ctx in contexts)` |
| **Per-account** | `_fanout_single_account(signal, handler, ctx, ...)` (`order_manager.py:437`): injects `account_open_limits`, calls `handler.build_intent(signal, account=ctx)` → `OrderIntent`. If `is_account_kill_switch_active(account_id)` and `action==OPEN` → `ValueError("KILL_SWITCH_ACTIVE")` short-circuit. Then `_evaluate_and_submit(intent, signal, handler, ...)` |
| **Model Blue sizing** | `ModelBlueStrategy._build_open_intent` (`app/services/model_blue/strategy.py:120`): checks duplicate via `trade_book.get(trade_id, account_id)`, creates per-account `ModelBlueSizer(TemporarySettingsCommittedCapitalProvider(ctx.committed_notional))`, `sizer.size_open(signal)` → 2 `SizedModelBlueLeg`s (`STK` floored to integer, non-STK quantized `0.0001`, `notional>=100`), then 2 `OrderLeg`s with `_STK_CONTRACT_MONTH="2026-09"` → `OrderIntent(signal_id=trade_id, strategy_id, action=OPEN, legs, account_id, ibkr_account, market)` |

### 5. RMS Checks — 5 Sequential Gates

| Item | Value |
|---|---|
| **Engine** | `RMSEngine.evaluate(intent, context)` (`app/rms/engine.py:48`): iterates `get_default_checks()` in order `2→3→4→7→8`, short-circuits on `REJECT/HALT`, carries `ADJUST` forward |
| **Audit** | `OrderManager._audit_rms(intent, rms_result, signal_pk)` (`order_manager.py:803`): `EventRepository.append(process="rms", kind=RMS_{PASS/REJECT}, detail={checks[...]})` idempotent `rms_{outcome}:{account}:{signal}` |
| **Checks** | See `docs/trading/rms.md` for per-check logic |

### 6. Exposure Guard + Execution Claim (Cross-Process Dedupe)

| Item | Value |
|---|---|
| **Exposure guard** | `OrderManager._exposure_guard(intent)` (`order_manager.py:652`): acquires sorted `asyncio.Lock` per `(account_id, symbol)` (`_get_exposure_lock`, `order_manager.py:642`) — held from RMS eval through exposure write, preventing cross-strategy same-symbol budget races |
| **Claim acquire** | `_acquire_execution_claim(intent)` (`order_manager.py:581`): own transaction `ExecutionClaimRepository.acquire(dedupe_key, ...)` where `dedupe_key = f"{account_id}:{strategy_id}:{signal_id}"` (`execution_claim_repository.py:35`). Duplicate `CLAIMED` within `stale_after_sec=300` raises `ExecutionInFlightError`; stale is reaped first. |
| **Sync hygiene** | `execute()` is wrapped in `asyncio.wait_for(OMS_TIMEOUT_SEC=300)` (`worker_pool.py:330`); on `TimeoutError` the claim is handled via `_resolve_failed_claim` / stale reconciliation so a later retry is not permanently blocked |

### 7. Instrument Resolution + OMS/Basket Execution

| Item | Value |
|---|---|
| **Resolve** | `OrderManager._resolve_instruments(intent, signal_pk)` (`order_manager.py:1094`): `ensure_cfd_instruments_for_symbols` (auto-discovers CFD via `request_contract_details`), `SnapshotInstrumentCatalog`, `attach_resolved(intent, catalog)` (`app/instruments/resolver.py:300`) → quantizes `quantity` via `size_increment`, validates `qty>0`, audits `INSTRUMENT_RESOLVED` event |
| **Basket path** | `BasketCoordinator.execute(intent, rms_result, order_type="MARKET", signal_pk)` (`app/oms/coordinator.py:122`) — see `docs/trading/oms.md` for full basket state machine |
| **OMS fallback** | If no baskets (`type(oms) is not OMSService` guard in `order_manager.py:160`), `OMSService.submit_intent(intent, rms_result, ...)` (`app/oms/oms_service.py:41`) submits leg-by-leg via `_submit_leg` → `adapter.submit_order` |
| **Pacing** | `IBKRExecutionAdapter.submit_order` (`app/oms/ibkr_adapter.py:179`) `await submit_pacer.acquire()` (0.2s min interval), dup check on `internal_order_id`, `client.placeOrder`, idempotent `adopt_order` |
| **Seal claim** | On success: `_seal_execution_claim(dedupe_key)` → `ExecutionClaimRepository.mark_executed` (`order_manager.py:603`); on failure: `_resolve_failed_claim` checks `count_orders_emitted`; if `>0` leaves `CLAIMED` for reconciliation, else `release` |

### 8. Broker Acknowledgement & Fills

| Item | Value |
|---|---|
| **Callbacks** | `IBKRExecutionAdapter` EWrapper listeners on TWS thread: `on_order_status` (`ibkr_adapter.py:417`), `on_open_order` (521), `on_exec_details` (567), `on_commission_report` (670), `on_error` (728), `on_connection_closed` (778) |
| **Status mapping** | `_map_ib_status` (`ibkr_adapter.py:348`): `PendingSubmit/PreSubmitted/Submitted → SUBMITTED`, `Filled → FILLED`, `Cancelled → CANCELLED`, `Inactive → REJECTED`, etc. Terminal guards prevent overwriting `FILLED/CANCELLED/REJECTED/ERROR`. |
| **Fill accounting** | `on_exec_details` creates `BrokerExecution(exec_id, quantity, price, perm_id)` per new `exec_id`, updates `filled_quantity` via `cumQty`, computes `average_fill_price` via `executions_weighted_average` or `avgFillPrice`; `on_commission_report` attaches `commission/realizedPNL` to the execution |
| **Future wake** | `_notify_future_if_terminal` (`ibkr_adapter.py:403`): resolves `loop.call_soon_threadsafe(future.set_result(order))` for waiters |
| **Wait** | `IBKRExecutionAdapter.wait_for_terminal_or_fill(internal_order_id, timeout)` (`ibkr_adapter.py:307`): `loop.create_future` + `asyncio.wait_for`; used by `BasketCoordinator._wait_terminals` (`coordinator.py:478`) per leg |

### 9. Position Lifecycle & Persistence

| Item | Value |
|---|---|
| **Basket persistence** | `BasketCoordinator._persist_basket` → `BasketRepository.upsert`; `_persist_child` → `OrderRepository.record_oms_order` + `ExecutionRepository.upsert` per execution; `_persist_broker_snapshot` on every broker callback (thread-safe via `run_coroutine_threadsafe`) (`coordinator.py:870`) |
| **Model Blue open** | `handler.after_submit` (`app/services/model_blue/strategy.py:80`) → `ModelBlueExecutionPersistence.persist_open` (`persistence.py:121`): requires enabled allocation, `TradeRepository.open_trade` → `PositionModel(account_id, trade_id)` + `SignalRepository.record_processed` + order records + `POSITION_OPEN` event |
| **Model Blue close** | Same hook → `persistence.persist_close`: `TradeRepository.close_trade(trade_id, exit_marks, commission)` + `POSITION_CLOSE` event, then `LivePnlService.unwatch` |
| **Runtime state** | `OrderManager._update_runtime_state` (`order_manager.py:925`): updates `processed_signals`, `open_positions`, `symbol_exposures` in `RMSContext`; on partial `CRITICAL`/`UNWINDING` uses `_record_unsettled_exposure` instead |

### 10. Reconciliation & Recovery

| Item | Value |
|---|---|
| **Startup recovery** | `RecoveryManager.run_startup_recovery` (`app/services/recovery.py:35`): scans `SignalJobModel WHERE status IN (CLAIMED,PROCESSING,RECOVERY_REQUIRED)` + `BasketModel WHERE state IN (EXECUTING,UNWINDING)`, best-effort `fetch_broker_order_snapshot`, reconciles stale claims, requeues `QUEUED` if `emitted==0` else quarantines `RECOVERY_REQUIRED`/`DEAD_LETTER`, then `hydrate_runtime_from_db` |
| **Periodic reconciler** | `PositionReconciler.run_once` (`app/services/position_reconciler.py:90`) every 30s: `request_positions_async` → `replace_snapshot(broker_positions)` → `build_ledger_net_lines` + `classify_reconcile_diffs` → `insert_run` + `POSITION_RECONCILE` event |

### 11. Frontend Streaming

| Item | Value |
|---|---|
| **Poll** | `PositionBridge.poll_once` (`demo_streaming/publisher.py:90`) every 2s: `load_position_rows` + `load_signals`, `position_leg_payloads` → fingerprint diff → `PositionStream.xadd` to Redis `positions:stream` |
| **SSE** | `GET /demo/stream` (`demo_streaming/api.py:44`): `XREAD` loop → `data: json\n\n`; `GET /demo/positions` snapshot for initial load |

## Normal Trading Flow — Sequence Diagram

```mermaid
sequenceDiagram
    participant TV as TradingView
    participant API as API :8000
    participant DB as Postgres
    participant W as Workers x10
    participant ENG as Trading Engine
    participant IBKR as IBKR Gateway

    TV->>API: POST /webhooks/tradingview
    API->>DB: create job (idempotent)
    API-->>TV: 202 accepted
    W->>DB: claim job + heartbeat
    W->>ENG: parse → fan-out accounts
    ENG->>ENG: RMS checks 2/3/4/7/8
    ENG->>DB: acquire claim + resolve
    ENG->>IBKR: Basket execute (paced 0.2s)
    IBKR-->>ENG: fills / commissions
    ENG->>DB: persist (OPEN/CLOSED or CRITICAL)
    W->>DB: COMPLETED / REJECTED
```

## Failure Paths

| Stage | Failure | System Behavior |
|---|---|---|
| Auth | Missing/invalid `X-Webhook-Secret` | 401, no DB write |
| Parse | Invalid JSON / missing `strategy_id` | 400 (ingestion) or `REJECTED` job status (worker) |
| Account routing | No enabled allocations for `strategy_id` | `ValueError` per account → `error` outcome, other accounts may still succeed |
| RMS | Any check REJECT | `ValueError("RMS check N rejected...")` → job status `REJECTED` |
| Exposure guard | Contention | Waits on `asyncio.Lock` per `(account_id, symbol)` |
| Execution claim | Duplicate in-flight | `ExecutionInFlightError` → job retried later (reclaim) |
| Instrument resolve | Missing CFD master row | `InstrumentResolutionError` → `REJECTED` order, `Error` propagation |
| Broker submit | Not connected / pacing | `ERROR` order status, `CRITICAL` if basket incomplete |
| Partial fill | Timeout (`fill_timeout`) | Retry loop (if paper + policy enabled) → compensation → `CRITICAL` |
| Worker lease lost | Reclaimed by another worker | `lease_lost` set; terminal write skipped, job will be retried |
| Worker crash | Process dies mid-job | Reclaimer promotes `CLAIMED/PROCESSING` → `QUEUED` (or `RECOVERY_REQUIRED` if `emitted>0`) on next interval; `RecoveryManager` handles crash on startup |

## Idempotency Layers

1. **HTTP ingestion**: `idempotency_key` unique constraint on `signal_jobs` — duplicate POST returns same `job_id` with `duplicate=true` (CSV) but no duplicate execution.
2. **Execution claim**: `execution_claims.dedupe_key` (`account:strategy:signal`) — cross-worker/cross-crash barrier; `CLAIMED→EXECUTED` lifecycle with stale reconciliation.
3. **Order dedupe**: `OMSService._submitted_signals` (`{account_id}:{signal_id}`) — in-memory guard for repeated `submit_intent` within the process.
4. **Basket trade**: `baskets` unique `(account_id, trade_id, action)` — at most one basket per trade action.
5. **Event idempotency**: `event_log.idempotency_key` unique — RMS/broker/basket events are upsert-safe.
6. **Worker lease fence**: `update_status(..., fence=True, worker_id)` checks `worker_id` + `lease_expires_at` — reclaimed jobs cannot overwrite terminal status.

---

## 4. Risk Management System (RMS)

> **Source file:** `docs/trading/rms.md`  —  original heading: *Risk Management System (RMS)*

> Verified against `backend/app/rms/engine.py`, `backend/app/rms/checks/*`, `backend/app/rms/models.py`, `backend/app/services/order_manager.py`.

## Overview

The RMS is a **synchronous, sequential gate** evaluated inside `OrderManager._evaluate_and_submit` **before** any broker submission. It is purely in-memory (no DB I/O during evaluation) and operates on an `RMSContext` hydrated at startup from PostgreSQL.

**Status: IMPLEMENTED** — 5 checks, numbered 2/3/4/7/8. Checks 1/5/6 do not exist in the codebase.

## Engine

| Item | Detail |
|---|---|
| **File** | `backend/app/rms/engine.py:37` |
| **Class** | `RMSEngine(checks: Sequence[BaseRMSCheck] \| None)` |
| **Default checks** | `get_default_checks()` → `[DuplicateCheck(2), StrategyCheck(3), ContractMonthCheck(4), OpenPositionLimitCheck(7), MoneyPerStockCheck(8)]` |
| **Method** | `evaluate(intent: OrderIntent, context: RMSContext) -> RMSResult` |
| **Flow** | Iterates `current_intent` through checks in order. On `REJECT/HALT` short-circuits. On `ADJUST` replaces `current_intent = adjusted_intent` and continues. On completion returns `PASS`. |
| **Result** | `RMSResult(outcome, intent=current_intent, original_intent, check_number, reason, check_results, timestamp)` (`app/rms/models.py`) |

```mermaid
flowchart LR
    S([Intent]) --> C2{"2 Duplicate"}
    C2 -- REJECT --> R([REJECT])
    C2 -- PASS --> C3{"3 Strategy"}
    C3 -- REJECT --> R
    C3 -- PASS --> C4{"4 Contract Month"}
    C4 -- ADJUST --> C7{"7 Positions"}
    C4 -- PASS --> C7
    C7 -- REJECT --> R
    C7 -- PASS --> C8{"8 Money"}
    C8 -- REJECT --> R
    C8 -- PASS --> P([PASS])
```

## RMS Context (`app/rms/models.py`)

| Field | Type | Source |
|---|---|---|
| `strategy_configs` | `dict[str, StrategyConfig]` | `OrderManager.__init__` default `max_open_positions=100, money_limit_per_symbol=10_000_000`; overridden per `(account_id,strategy_id)` via `account_open_limits` |
| `processed_signals` | `set[tuple[str,str]]` or `set[tuple[int,str,str]]` | `SignalRepository.list_processed_open_keys` + account-scoped keys via `account_router` |
| `open_positions` | `dict[key, int]` | `PositionRepository.list_open` count by `(account_id,strategy_id)` |
| `symbol_exposures` | `dict[(account_id,symbol), Decimal]` | `_add_row_exposure` per open `PositionModel` leg (abs qty × entry mark) |
| `per_symbol_limits` | `dict[(account_id,symbol), Decimal]` | `PerSymbolLimitModel` rows |
| `default_symbol_limits` | `dict[account_id, Decimal]` | `AccountModel.default_symbol_limit` |
| `account_open_limits` | `dict[(account_id,strategy_id), int]` | `AllocationModel.max_open_positions` per account×strategy |
| `rollover_checker` / `target_rollover_month` | calendar logic | Injected; default uses `is_default_rollover_active` |

Hydrated by `OrderManager.hydrate_runtime_from_db` (`order_manager.py:171`) and reloaded via `reload_rms_limits` after config mutations.

## Check Reference

### Check 2 — DuplicateCheck

| Item | Detail |
|---|---|
| **File** | `backend/app/rms/checks/duplicate.py:7` |
| **Class** | `DuplicateCheck(BaseRMSCheck)` — `check_number=2`, `check_name="DUPLICATE"` |
| **Method** | `evaluate(intent, context) -> CheckResult` |
| **Input** | `intent.strategy_id`, `intent.signal_id`, `intent.account_id`, `context.processed_signals` |
| **Logic** | If `action==CLOSE` → `PASS` (closes are never duplicates). Else lookup key = `(strategy_id, signal_id)` or `(account_id, strategy_id, signal_id)` if `account_id` present. If key in `processed_signals` → `REJECT "DUPLICATE_SIGNAL"` else `PASS`. |
| **Reject reason** | `DUPLICATE_SIGNAL` |
| **Status** | **IMPLEMENTED** |

### Check 3 — StrategyCheck

| Item | Detail |
|---|---|
| **File** | `backend/app/rms/checks/strategy.py:14` |
| **Class** | `StrategyCheck` — `check_number=3`, `check_name="STRATEGY"` |
| **Method** | `evaluate(intent, context) -> CheckResult` |
| **Logic** | If `CLOSE` or `EMERGENCY_FLATTEN` (`intent.intent_mode`) → `PASS`. Else if `strategy_id` blank/missing → `REJECT "MISSING_STRATEGY_ID"`. Else if `strategy_id not in context.strategy_configs` → `REJECT "UNKNOWN_STRATEGY"`. Else `PASS`. |
| **Reject reasons** | `MISSING_STRATEGY_ID`, `UNKNOWN_STRATEGY` |
| **Status** | **IMPLEMENTED** |

### Check 4 — ContractMonthCheck (ADJUST)

| Item | Detail |
|---|---|
| **File** | `backend/app/rms/checks/contract_month.py:58` |
| **Class** | `ContractMonthCheck` — `check_number=4`, `check_name="CONTRACT_MONTH"` |
| **Method** | `evaluate(intent, context) -> CheckResult` |
| **Input** | Only legs where `is_expiry_instrument(leg.instrument_type)` (`FUT/FOP/OPT`) are examined; non-expiry legs are ignored |
| **Logic** | If no expiry legs → `PASS`. If expiry legs have mismatched `contract_month` or `should_rollover` is true (for `OPEN`: uses `context.rollover_checker` or `target_rollover_month` or default `is_default_rollover_active(days_remaining ≤ window)`), then `target = target_rollover_month or get_next_contract_month(first_month) or first_month`; rewrites legs via `replace(contract_month=target)` → `ADJUST` with new `OrderIntent` and `CONTRACT_MONTH_ROLLOVER`. Else `PASS`. |
| **Helpers** | `get_next_contract_month(YYYY-MM)`, `is_default_rollover_active` (`contract_month.py:18,37`) |
| **Outcome** | `PASS` or `ADJUST` (never REJECT). The engine replaces `current_intent` and continues. |
| **Status** | **IMPLEMENTED** (no-op for STK/CFD/ETF trades which never trigger the expiry path) |

### Check 7 — OpenPositionLimitCheck

| Item | Detail |
|---|---|
| **File** | `backend/app/rms/checks/position_limit.py:14` |
| **Class** | `OpenPositionLimitCheck` — `check_number=7`, `check_name="OPEN_POSITION_LIMIT"` |
| **Method** | `evaluate(intent, context) -> CheckResult` |
| **Logic** | If `CLOSE` → `PASS`. Else requires `strategy_configs[strategy_id]`; missing → `REJECT "MISSING_STRATEGY_CONFIG"`. `current = context.open_positions[open_position_key(intent)]` where key `(account_id, strategy_id)`. `max_positions = strategy_cfg.max_open_positions` overridden by `context.account_open_limits[(account_id,strategy_id)]` if present. If `current >= max` → `REJECT "OPEN_POSITION_LIMIT_REACHED"` else `PASS`. |
| **Audit detail** | `MONEY_LIMIT_EXCEEDED` / `OPEN_POSITION_LIMIT_REACHED` include `current/max` counts in `RMSResult` |
| **Status** | **IMPLEMENTED** |

### Check 8 — MoneyPerStockCheck

| Item | Detail |
|---|---|
| **File** | `backend/app/rms/checks/money_per_stock.py:17` |
| **Class** | `MoneyPerStockCheck` — `check_number=8`, `check_name="MONEY_PER_STOCK"` |
| **Method** | `evaluate(intent, context) -> CheckResult` |
| **Logic** | If `CLOSE` or `EMERGENCY_FLATTEN` → `PASS`. Else aggregates `symbol_order_notionals[leg.symbol] += leg.effective_notional` (`leg.notional` or `price×qty`). If empty → `PASS`. Per symbol resolves `limit_per_symbol`: `per_symbol_limits[(account_id,symbol)]` else `default_symbol_limits[account_id]` else `strategy_cfg.money_limit_per_symbol`; if `None` skip. `existing = context.symbol_exposures[(account_id,symbol)]`. If `existing + order_notional > limit` → `REJECT "MONEY_LIMIT_EXCEEDED"` else `PASS`. |
| **Priority** | Per-symbol limit has 3-tier precedence: row override → account default (`default_symbol_limit`, default `10_000_000`) → strategy fallback |
| **Status** | **IMPLEMENTED** |

## Rejection Behavior

* On `REJECT`, `RMSEngine.evaluate` returns `RMSResult(REJECT, check_number, reason)` immediately. No later checks run.
* Caller (`OrderManager._evaluate_and_submit_locked:692`) calls `_audit_rms`, then `raise ValueError(f"RMS check {n} rejected...")` which bubbles to `_fanout_single_account`, captured as `AccountExecutionOutcome(error=...)`. `_process_signal_execution` (`order_manager.py:355`) persists the signal as `REJECTED` with aggregated reason and (for single-account) re-raises.
* Worker writes job `REJECTED` (`worker_pool.py:277`).

## Interaction with Other Subsystems

* **Kill switch** is checked **before** RMS (`order_manager.py:437`): `is_account_kill_switch_active(account_id)` + `action==OPEN` → immediate `ValueError` without invoking RMS.
* **Exposure guard** (`_exposure_guard`) wraps RMS eval + exposure write — the `RMSEngine` itself is stateless and not concurrent-safe for shared `RMSContext`; the guard serializes per `(account_id,symbol)`.
* **BasketCoordinator** re-evaluates RMS during retry/compensation via `rms_engine.evaluate` (`coordinator.py:539`) — retry legs that would breach `MoneyPerStock` are skipped and marked blocked.

## Not Implemented

| Potential check | Status |
|---|---|
| Global notional / max loss | **NOT FOUND** |
| Order rate limit | **NOT FOUND** (pacing is in OMS `OrderSubmitPacer`, not RMS) |
| Instrument whitelist/blacklist | **NOT FOUND** |
| Price deviation / fat-finger | **NOT FOUND** |
| Checks 1 / 5 / 6 | **NOT FOUND** (numbering is non-contiguous: only 2,3,4,7,8 exist) |

## Base Class

`BaseRMSCheck` (`app/rms/checks/base.py:8`): `ABC` with `check_number: int`, `check_name: str`, abstract `evaluate(intent, context) -> CheckResult`. `CheckResult` carries `outcome` (`PASS|REJECT|HALT|ADJUST`), `reason`, `adjusted_intent`.

## Related Files

* Models: `backend/app/rms/models.py` — `OrderLeg`, `OrderIntent`, `RMSContext`, `StrategyConfig`, `RMSResult`, `CheckResult`, `OrderSide`, `RMSOutcome`
* Hydration: `backend/app/services/order_manager.py:171` (`hydrate_runtime_from_db`, `_apply_symbol_limits`, `reload_rms_limits`)
* Audit: `backend/app/services/order_manager.py:803` (`_audit_rms` → `event_log`)

---

## 5. Order Management System (OMS)

> **Source file:** `docs/trading/oms.md`  —  original heading: *Order Management System (OMS)*

> Verified against `backend/app/oms/oms_service.py`, `backend/app/oms/coordinator.py`, `backend/app/oms/basket.py`, `backend/app/oms/models.py`, `backend/app/oms/ibkr_adapter.py`, `backend/app/services/order_manager.py`.

## Overview

The OMS is the post-RMS layer that turns a validated `OrderIntent` (N legs) into durable IBKR orders with atomic basket semantics: either **all legs fill** or the system cancels/compensation-closes and marks `CRITICAL` to block future opens.

Two classes cooperate:

* **`OMSService`** (`oms_service.py:17`) — in-memory leg-level submission, order map, dedupe.
* **`BasketCoordinator`** (`coordinator.py:48`) — database-backed basket state machine, retry, compensation, `CRITICAL` escalation.

## Order Lifecycle

```mermaid
flowchart LR
    I["Intent (N legs)"] --> B["Basket EXECUTING"]
    B --> S["Submit N legs<br/>pacer 0.2s"]
    S --> W["Wait fills"]
    W --> C{"All filled?"}
    C -- yes --> OK["OPEN / CLOSED"]
    C -- no --> R{"Retry? (paper)"}
    R -- yes --> W
    R -- no --> U["UNWINDING<br/>cancel + compensate"]
    U --> F{"Compensated?"}
    F -- yes --> CP["COMPENSATED"]
    F -- no --> CR["CRITICAL"]
```

## OMSService — Leg-Level Submission

| Item | Detail |
|---|---|
| **File** | `backend/app/oms/oms_service.py:17` |
| **Constructor** | `OMSService(adapter: IBKRExecutionAdapter)` — owns `_orders: dict[str,OMSOrder]`, `_submitted_signals: set[str]` |
| **submit_intent** | `async submit_intent(intent, rms_result, override_internal_id, limit_price, order_type="LIMIT") -> ExecutionResult` — validates `RMS PASS`, checks dedupe `f"{account_id}:{signal_id}"`, loops legs via `_submit_leg`, returns `ExecutionResult(order=legs[0], success=not first_error, orders=[...])` |
| **submit_one_leg** | `async submit_one_leg(intent, rms_result, index, oms_received_at, override_internal_id, limit_price, order_type) -> OMSOrder` — single-leg handoff; used by `BasketCoordinator` per leg |
| **_submit_leg** | `async _submit_leg(..., index) -> OMSOrder` — validates `leg_order_id`, computes `_leg_limit_price` (only non-None for single-leg), resolves instrument (`leg.resolved or resolve_leg`), creates `OMSOrder(status=PENDING, parent_signal_id, leg_index, resolved)`, stores `_orders`, calls `adapter.submit_order(order)` (exception → `ERROR`) |
| **_leg_order_id** | `def _leg_order_id(signal_id, index, leg_count, override_internal_id, account_id) -> str` — `f"{account_id}-ORD-{prefix}{signal_id}[-L{index}]"` |
| **_leg_limit_price** | `def _leg_limit_price(leg, limit_price, leg_count) -> Decimal\|None` — `limit_price` only for single-leg, else `leg.price` |
| **get/cancel** | `get_order(id)`, `get_all_orders()`, `cancel_order(id) -> adapter.cancel_order` |

In-memory only — no DB. Persistence is delegated to `BasketCoordinator._persist_child`.

## BasketCoordinator — Atomic Basket State Machine

| Item | Detail |
|---|---|
| **File** | `backend/app/oms/coordinator.py:48` |
| **Constructor** | `BasketCoordinator(oms, session_factory, fill_timeout=30.0, cancel_timeout=30.0, retry_policy, rms_engine, rms_context, paper_retries_allowed)` — owns `_critical: set[(account_id,strategy_id)]`, `_order_baskets: dict[internal_id→Basket]`, registers `adapter.add_order_state_listener(_on_broker_order_state)` |
| **Basket model** | `app/oms/basket.py:23` — `@dataclass Basket(account_id, trade_id, strategy_id, action, intended_leg_count, state:BasketState, id, signal_pk, orders, compensation_orders)` |
| **States** | `PENDING → EXECUTING → OPEN / CLOSED` (success) / `UNWINDING → COMPENSATED / CRITICAL` (failure). See `basket.py:11`. |

### execute — Core Method (`coordinator.py:122`)

1. `attach_resolved(intent, catalog)` (re-resolve to ensure resolved is fresh)
2. Create `Basket(state=EXECUTING)` → `_persist_basket` (`BasketRepository.upsert`) → emit `BASKET_CREATED`/`BASKET_EXECUTING`
3. Loop legs (`index 0..N-1`): `oms.submit_one_leg` with `abort_remaining` on `REJECTED/ERROR` → `_persist_child` (non-compensation) + `ORDER_CREATED`/`ORDER_SUBMITTED` events (CLOSE variants for `action==CLOSE`)
4. `_wait_terminals(orders, fill_timeout)` — gather `adapter.wait_for_terminal_or_fill` per non-terminal order
5. Check `_basket_complete(intent, orders)` (`filled+EPS ≥ leg.quantity` per leg). If complete → set `OPEN` (or `CLOSED` if `CLOSE`) → persist → emit `BASKET_OPEN`/`BASKET_CLOSED` → return `BasketExecutionResult(state=OPEN/CLOSED, success=true)`
6. If incomplete and `_retries_enabled()` (requires `policy.enabled`, `max_retries>0`, `paper_retries_allowed`, `rms_engine+rms_context` wired) → `_retry_incomplete`
7. If still incomplete → `UNWINDING`: `_cancel_working` on all non-terminal, wait `cancel_timeout`, if still working → `_fail_critical(CRITICAL)`, else `_compensate_filled` (reverse filled legs as `CLOSE` orders with synthetic `RMS PASS`), wait, if not `COMPENSATED` → `CRITICAL`
8. `CRITICAL` calls `mark_critical(account_id, strategy_id)` → future `is_open_blocked` returns true, blocking new OPENs in `OrderManager`.

### Retry (`coordinator.py:539`)

* Cancels working orders first, then loops `attempt < max_retries` within `retry_window_sec`.
* Per-leg `remaining = leg.quantity - _filled_qty_for_leg(index)`.
* Skips via `retry_key={account}:{signal}:{index}:{attempt}` dedupe in `_retry_ids`.
* Builds `retry_intent` (`_retry_intent`: single-leg, `signal_id="{orig}:RETRY:L{index}:{attempt}"`, preserves `EMERGENCY_FLATTEN` if original was close/emergency).
* Re-checks RMS (`rms_engine.evaluate`); `REJECT` → block index and emit `AUTO_SQUARE_OFF_RETRY_BLOCKED`.

### Compensation (`coordinator.py:747`)

* For each leg with `cum_filled > EPS`: creates reverse `OrderIntent(action=CLOSE, signal_id="{orig}:UNWIND:L{index}")`, bypasses RMS with synthetic `PASS`, `oms.submit_intent(comp_intent, pass_rms)`, marks `is_compensation=True`, `_persist_child(compensation=True)` → emits `COMPENSATION`.

### Critical (`coordinator.py:839`)

`_fail_critical` sets `Basket(CRITICAL)`, adds `(account_id,strategy_id)` to `_critical`, persists, emits `BASKET_CRITICAL`. `RECOV` path (`recover_incomplete_baskets:404`) marks all incomplete baskets `CRITICAL` after `fetch_broker_order_snapshot`.

### Other Methods

| Method | Purpose |
|---|---|
| `is_open_blocked(account_id, strategy_id) -> bool` | Checks `_critical` |
| `mark_critical(account_id, strategy_id)` | Inserts into `_critical` |
| `apply_retry_policy(policy, paper_retries_allowed)` | Sets `fill_timeout = square_off_after_sec` |
| `hydrate_critical_from_db()` | `BasketRepository.list_critical` → rebuild `_critical` |
| `recover_incomplete_baskets()` | Lists `BasketRepository.list_incomplete`, snapshot-or-critical |
| `_on_broker_order_state(order, kind)` | Filters `BROKER_ACK/PARTIAL_FILL/FILL/...`, schedules `_persist_broker_snapshot` via `run_coroutine_threadsafe` |
| `_persist_broker_snapshot(order, kind)` | `_persist_child` + event `kind` with idempotency `broker_ack:{id}`, `fill:{id}`, `partial:{exec_id}\|{id}:{qty}` |

## Order Intent & Legs

| Model | File | Fields |
|---|---|---|
| `OrderIntent` | `app/rms/models.py` | `signal_id, strategy_id, action (OPEN/CLOSE), legs: list[OrderLeg], account_id, ibkr_account, market, intent_mode (NORMAL/EMERGENCY_FLATTEN), resolved snapshot` |
| `OrderLeg` | `app/rms/models.py` | `symbol, side (BUY/SELL), quantity, price, instrument_type, contract_month, notional, market, currency, leg_index, conId` |
| `ResolvedInstrument` | `app/instruments/models.py:20` | `symbol, sec_type (STK/CFD), exchange, currency, con_id, market_data_conid, multiplier, size_increment` |

`OrderIntent` is built by `ModelBlueStrategy.build_intent` (or legacy path) and mutated only by `ContractMonthCheck` (ADJUST) and `attach_resolved`.

## OMS Domain Models

| Model | File | Detail |
|---|---|---|
| `OMSOrder` | `app/oms/models.py:118` | `internal_order_id`, `intent`, `symbol/side/quantity`, `status:OMSOrderStatus`, `filled_quantity`, `average_fill_price`, `limit_price`, `basket_id`, `is_compensation`, `executions: dict[exec_id→BrokerExecution]`, `timestamps: ExecutionTimestamps`, `resolved`, `pacer_delayed` |
| `BrokerExecution` | `oms/models.py:78` | `exec_id, internal_order_id, symbol/side, quantity, price, commission, realized_pnl, perm_id, executed_at` |
| `ExecutionResult` | `oms/models.py:175` | `order: OMSOrder, rms_result, success, error_message, orders: list[OMSOrder]` |
| `ExecutionTimestamps` | `oms/models.py:28` | 8 boundary timestamps + latency properties (`rms_latency_ms`, `ibkr_submit_latency_ms`, `submit_to_fill_ms`, …) |
| `FanoutExecutionResult` | `oms/models.py:204` | `outcomes: list[AccountExecutionOutcome]` — aggregates per-account `ExecutionResult`s |
| `Basket` / `BasketExecutionResult` | `app/oms/basket.py:23,40` | Pure domain, no DB |

Order status enum (`OMSOrderStatus:12`): `PENDING → SUBMITTED → PARTIALLY_FILLED → FILLED` success path; `CANCELLED / REJECTED / ERROR` terminal failures.

## Fills, Partial Fills, Rejected Orders

| Scenario | Handling |
|---|---|
| **Full fill** | `on_exec_details` per `exec_id` increments `filled_quantity` (`cumQty` or `exec.shares`), `executions_weighted_average` for `average_fill_price`; `on_order_status` maps to `FILLED` → `_notify_future_if_terminal` wakes `wait_for_terminal_or_fill` |
| **Partial fill** | Same as above but `filled < quantity`; `_basket_complete` fails → retry/compensation/CRITICAL branch |
| **Rejected** | `on_error` errorCode in `{200,201,10147,10148,10243}` → `REJECTED`; `_submit_leg` catches `InstrumentResolutionError` → `REJECTED` OMSOrder; basket aborts remaining legs |
| **Cancelled** | `on_error` code `202` → `CANCELLED`; `cancel_order` via `client.cancelOrder` |
| **Error** | Other errorCodes → `ERROR`; `on_connection_closed` marks all non-terminal `ERROR` |

## Cancellations

`OMSService.cancel_order(id)` → `IBKRExecutionAdapter.cancel_order` → `client.cancelOrder(tws_order_id)`. Used by `BasketCoordinator._cancel_working` for all non-terminal legs during unwind/retry.

## Position Effects

The OMS does **not** directly mutate `positions`. It persists `orders`/`executions`/`baskets`/`event_log`. Position rows are managed by `ModelBlueExecutionPersistence.persist_open/close` called from `StrategyHandler.after_submit` after `BasketCoordinator.execute` returns success.

## Persistence

| Call | Table | When |
|---|---|---|
| `BasketRepository.upsert` | `baskets` | Basket create / state transition / critical |
| `OrderRepository.record_oms_order` | `orders` | Per leg submit + per broker snapshot (upsert on `internal_order_id`, preserves terminal status) |
| `ExecutionRepository.upsert` | `executions` | Per new `exec_id` in `on_exec_details`, per commission in `on_commission_report` |
| `EventRepository.append` | `event_log` | `BASKET_*`, `ORDER_*`, `BROKER_ACK`, `FILL`, `PARTIAL_FILL`, `COMMISSION`, `AUTO_SQUARE_OFF_*`, `COMPENSATION` — all with `idempotency_key` |

## Error Handling Summary

* Missing instrument master row for CFD → `InstrumentResolutionError` → `REJECTED` order, basket aborts.
* `adapter.submit_order` exception → `ERROR` order.
* All basket persistence uses `run_coroutine_threadsafe` from TWS thread → exceptions logged but do not crash the socket reader.
* `CRITICAL` is the terminal containment: no orders are auto-submitted after marking critical until operator `reload_rms_limits` or restart + `list_critical` hydration (still blocked).

## OMS Sequence Diagram

```mermaid
sequenceDiagram
    participant OM as OrderManager
    participant BC as BasketCoordinator
    participant IBKR as IBKR Stack

    OM->>BC: execute(intent, MARKET)
    BC->>BC: upsert EXECUTING + resolve
    loop N legs
        BC->>IBKR: submit (paced 0.2s)
        IBKR-->>BC: SUBMITTED / fills
    end
    BC->>BC: wait fill_timeout
    alt all filled
        BC-->>OM: OPEN / CLOSED
    else partial
        BC->>BC: retry or unwind
        BC-->>OM: COMPENSATED or CRITICAL
    end
```

---

## 6. Position Reconciliation

> **Source file:** `docs/trading/position-reconciliation.md`  —  original heading: *Position Reconciliation*

> Verified against `backend/app/services/position_reconciler.py:1`, `backend/app/services/reconcile_service.py:55`, `backend/app/broker/ibkr/positions.py`, `backend/app/db/models/broker_position.py`, `backend/app/db/repositories/broker_position_repository.py`.

## Overview

The system does **not** auto-correct positions. Reconciliation is **snapshot + log + expose diffs** only. Every 30 seconds a background task fetches the broker's true positions, persists a point-in-time snapshot, nets the ledger's `OPEN` rows per `(account_id, symbol, sec_type)`, classifies mismatches, and writes a `position_reconcile_runs` row + `event_log` entry.

| Property | Value |
|---|---|
| **Mode** | Read-only classification; no ledger mutation |
| **Interval** | `RECONCILE_INTERVAL_SEC = 30.0` (`position_reconciler.py:27`) |
| **Timeout** | `POSITIONS_REQUEST_TIMEOUT_SEC = 15.0` |
| **Epsilon** | `QTY_EPSILON = 1e-6` |
| **In-flight tolerance** | Accounts with `EXECUTING`/`UNWINDING` baskets or `PROCESSING` jobs are flagged `in_flight=true` on diffs |
| **Status** | **IMPLEMENTED** |

## Components

| Component | File | Role |
|---|---|---|
| `PositionReconciler` | `app/services/position_reconciler.py:247` | Periodic loop: `request_positions` → snapshot → diff → run row + event |
| `BrokerPositionLine` | `app/broker/ibkr/positions.py` | `ibkr_account, symbol, sec_type, con_id, currency, exchange, quantity, avg_cost` |
| `BrokerPositionRepository` | `app/db/repositories/broker_position_repository.py:13` | `replace_snapshot`, `insert_run`, `list_snapshot`, `get_latest_run` |
| `Reconcile helper` | `app/services/reconcile_service.py:55` | Read-only `collect_reconcile_positions` for `GET /reconcile/positions` (does NOT call IBKR) |
| `BrokerFlattenService` | `app/services/broker_flatten_service.py:35` | Optional single-line `MKT` flatten for `BROKER_ORPHAN` diffs (operator-initiated) |

## Broker Positions

*Source: `TWSClient.request_positions_async(timeout=15.0)` → `BrokerPositionLine` per `conId`.*

Fetched via `TWSClient.request_positions` (`app/broker/ibkr/tws_client.py:408`): `cancelPositions` pre+post, `reqPositions()`, collector `wait(timeout)` → `(lines, timed_out)`. If `client.is_connected()==False`, the sweep is skipped entirely (`run_once:310`).

Persisted via `BrokerPositionRepository.replace_snapshot(rows, as_of)` (`broker_position_repository.py:13`): `DELETE FROM broker_positions` + `INSERT` per line (composite PK `(ibkr_account, conId)`). Only when `error is None`; timeout still persists (with `timed_out=true`).

## Internal (Ledger) Positions

*Source: `PositionModel WHERE risk_state='OPEN'` netted per `(account_id, symbol, sec_type)`.*

`build_ledger_net_lines(open_rows, instruments)` (`position_reconciler.py:87`):

1. Build `symbol_to_conids: dict[(symbol, sec_type) → set[conId]]` from `InstrumentModel`.
2. For each `PositionModel` open row, iterate both legs `(leg_a_symbol, leg_a_instrument_type, leg_a_signed_qty)` + `(leg_b_*)`. Normalize `sec_type` (default `STK`). Sum `nets[(account_id, symbol, sec_type)] += signed_qty`.
3. Drop nets with `abs(qty) ≤ 1e-6`. Attach `con_ids` from instrument map. Return `list[LedgerNetLine(account_id, symbol, sec_type, signed_qty, con_ids)]`.

Note: ledger netting is **symbol + sec_type scoped**, not per-`trade_id`. Two pair trades on the same symbol net together for reconciliation.

## Diff Classification

`classify_reconcile_diffs(broker_lines, ledger_lines, ibkr_to_account, timed_out, in_flight_accounts)` (`position_reconciler.py:128`):

| Kind | Condition | `broker_qty` | `ledger_qty` |
|---|---|---|---|
| `MATCH` | Both sides present and `abs(broker-ledger) ≤ 1e-6` | present | present |
| `QTY_DRIFT` | Both present but `abs(diff) > 1e-6` | present | present |
| `BROKER_ORPHAN` | Broker position exists, no ledger net | present | `None` |
| `LEDGER_GHOST` | Ledger net exists, no broker position; **suppressed if `timed_out==true`** | `None` | present |
| `UNMAPPED_ACCOUNT` | Broker `ibkr_account` not in `accounts` table | present | `None` (separate bucket) |

`in_flight = account_id in in_flight_accounts` (`fetch_in_flight_accounts`: `BasketModel.state IN (EXECUTING,UNWINDING)` + `SignalJobModel.status==PROCESSING`). Flagged on every non-`UNMAPPED` diff but does not suppress the diff.

`all_keys = broker_keys ∪ ledger_keys` sorted; each key produces exactly one primary diff (plus `UNMAPPED` extras).

## Persistence

After classification, `_persist_and_diff` (`position_reconciler.py:336`) writes:

* **`broker_positions`** snapshot (if no fetch error) — `REPLACE` semantics.
* **`position_reconcile_runs`** — `insert_run(started_at, finished_at, timed_out, error, broker_line_count, match_count, ghost_count, orphan_count, drift_count, unmapped_count, mismatches=[non-MATCH diffs])`.
* **`event_log`** — `append(process="reconcile", kind="POSITION_RECONCILE", detail={run_id, timed_out, error, counts, mismatches}, idempotency_key=f"reconcile:{run_id}")`.

Logging: `WARNING` if any mismatch/timeout/error, else `INFO "all matched"`.

## API Exposure

`GET /api/v1/reconcile/positions?ibkr_account=...` (`app/api/routes/reconcile.py:18`) calls `collect_reconcile_positions(session, ibkr_account)` (`reconcile_service.py:55`) — purely DB reads: `get_latest_run`, `list_snapshot`, `list_open positions + instruments`, `fetch_in_flight_accounts`, `build_ledger_net_lines` + `classify_reconcile_diffs` → `ReconcilePositionsResponse(run, broker_positions, ledger_positions, diffs)`. No `reqPositions` call.

`POST /api/v1/reconcile/positions/flatten` (`reconcile.py:28`) delegates to `BrokerFlattenService.flatten_line` — submits a single `MKT` closing order for one `BROKER_ORPHAN` line (emergency flatten, deduped via `_IN_FLIGHT_BROKER_FLATTENS`).

## Background Execution

```
PositionReconciler.start()  (app/main.py:110)
        │
        ├── _loop() every 30s
        │     └── run_once()
        │           ├── if _sweep_lock locked → skip
        │           ├── if not client.is_connected() → skip
        │           ├── request_positions_async(timeout 15s)
        │           └── _persist_and_diff(...)
        │
        └── stop() cancels task
```

No retry of the `reqPositions` call; timeout is recorded as `timed_out=true` and the next sweep runs on schedule.

## Recovery Behavior

* After a crash, `RecoveryManager` does **not** trigger an immediate reconcile sweep. The `PositionReconciler` resumes its 30s loop on next startup.
* Durable `broker_positions` snapshot survives restarts and is available to the `GET /reconcile/positions` endpoint even if IBKR is offline.

## Failure Scenarios

| Failure | Detection | System Behavior |
|---|---|---|
| TWS disconnected | `client.is_connected()==False` at sweep start | Sweep skipped, no DB write |
| `reqPositions` timeout | `timed_out==True` | Snapshot persisted, `LEDGER_GHOST` suppressed (avoid false ghost on incomplete broker data), run marked `timed_out` |
| `reqPositions` exception | `error=str(exc)` | Snapshot NOT replaced (stale snapshot retained), run row + event still written with `error` |
| DB failure during `_persist_and_diff` | `session.begin()` exception | Exception propagates to `_loop` → `logger.exception("Position reconcile sweep failed")`, next sweep retries |
| IBKR account unmapped | `ibkr_account not in ibkr_to_account` | Diff `UNMAPPED_ACCOUNT`, not counted as orphan/ghost |

## Diagram

```mermaid
sequenceDiagram
    participant RC as Reconciler 30s
    participant GW as Gateway
    participant DB as Postgres

    loop every 30s
        RC->>GW: reqPositions
        GW-->>RC: snapshot
        RC->>DB: snapshot + diff + run
    end
```

---

# PART III — INTEGRATIONS


---

## 7. IBKR / Broker Integration

> **Source file:** `docs/integrations/ibkr.md`  —  original heading: *IBKR Integration*

> Source: `backend/app/oms/ibkr_adapter.py:43`, `backend/app/broker/ibkr/tws_client.py:16`, `backend/app/broker/ibkr/scheduler.py`, `backend/app/instruments/*`, `backend/app/core/config.py`, `backend/app/oms/submit_pacer.py`, `backend/app/oms/retry_policy.py:7`

## Overview

One `TWSClient` socket → one `IBKRExecutionAdapter` → one `OrderSubmitPacer(0.2s)`. All accounts share the socket; `ib_order.account` tags routing (`backend/app/oms/ibkr_adapter.py:175`). No multi-gateway pool exists.

## Config

`backend/app/core/config.py:36`:

| Var | Default | Notes |
|-----|---------|-------|
| `ibkr_host` | `127.0.0.1` | TWS/Gateway host |
| `ibkr_port` | `7497` | Paper TWS default. EC2 Gateway paper uses `4002` |
| `ibkr_client_id` | `1` | Unique per socket session |
| `ibkr_connection_timeout` | `10` | Seconds to wait for `nextValidId` handshake |
| `ibkr_market_data_type` | `3` | `1`=live, `3`=delayed (default) |
| `ibkr_market_data_symbol/sec_type/exchange/currency/primary_exchange` | `AAPL/STK/SMART/USD/None` | Market-data subscription defaults |

Paper retry gate: `paper_retry_ports_allowed()` in `backend/app/oms/retry_policy.py:10` allows only `{7497, 4002}` for basket retries / square-off. Other ports do not retry.

## TWSClient — `backend/app/broker/ibkr/tws_client.py:16`

`class TWSClient(EWrapper, EClient)` — low-level transport. No business rules.

### State

- `next_order_id: int | None` — set by `nextValidId` (`tws_client.py:63`).
- `_connected_event: threading.Event` — handshake completion signal.
- `_thread: Thread | None` — daemon `TWSClientThread` running `EClient.run()` (`tws_client.py:506`).
- `_listeners: list[Any]` / `_market_data_listeners: list[Any]` — fan-out targets (`register_listener` `tws_client.py:365`, `register_market_data_listener` `tws_client.py:361`).
- `_request_types: dict[int,str]` + `_registry_lock` — `register_request_id` / `get_request_type` / `unregister_request_id` (`tws_client.py:46`).
- `_contract_details_events/results` + `_contract_details_lock` — blocking `request_contract_details`.
- `_position_collector: PositionSnapshotCollector` + `_positions_request_lock` — `request_positions`.

### Connection lifecycle

| Method | Location | Behavior |
|--------|----------|----------|
| `is_connected()` | `tws_client.py:465` | `isConnected() and _connected_event.is_set()` (both required) |
| `connect_and_start(host,port,client_id,timeout)` | `tws_client.py:469` | Idempotent if already connected (`tws_client.py:485`). Calls `EClient.connect`, spawns daemon thread, blocks on `_connected_event.wait(timeout)`. On timeout calls `disconnect_clean()` and returns `False`. |
| `disconnect_clean()` | `tws_client.py:524` | `disconnect()`, `join(timeout=2.0)`, clear `_connected_event`/`next_order_id`, clear `_request_types`, wake pending contract-details events. |

### Request helpers

| Method | Location | Notes |
|--------|----------|-------|
| `request_contract_details(contract, timeout=5.0)` | `tws_client.py:369` | Allocates `reqId` from `60000+`, registers event, `reqContractDetails`, blocks on `threading.Event.wait`. Returns `list[Any]`. |
| `request_contract_details_async` | `tws_client.py:401` | `asyncio.to_thread` wrapper. |
| `request_positions(timeout=15.0)` | `tws_client.py:407` | Resets collector, `register_listener(collector)`, `cancelPositions()` before/after, `reqPositions()`, `collector.wait(timeout)`. Returns `(list[BrokerPositionLine], timed_out)`. |
| `request_positions_async` | `tws_client.py:451` | `asyncio.to_thread` wrapper. |

`BrokerPositionLine` / `PositionSnapshotCollector` in `backend/app/broker/ibkr/positions.py:8` — collector ignores zero-qty lines, accumulates until `positionEnd`.

### EWrapper callbacks (fan-out)

All callbacks call `super()` then iterate `_listeners` and/or `_market_data_listeners` with `hasattr` guard:

- `nextValidId(orderId)` `tws_client.py:63` — sets `next_order_id`, sets `_connected_event`.
- `error(reqId, errorCode, errorString)` `tws_client.py:76` — codes `2000-2999` are status (info/debug), else warning. Forwards to both listener lists via `on_error`.
- `connectionClosed()` `tws_client.py:122` — clears `_connected_event`, `next_order_id=None`, fans out `on_connection_closed`.
- `tickPrice/tickSize/marketDataType/rerouteMktDataReq` `tws_client.py:140` — market-data listeners only.
- `contractDetails/contractDetailsEnd` `tws_client.py:178` — accumulates results under lock, sets event.
- `accountSummary/accountSummaryEnd/position/positionEnd` `tws_client.py:208` — general listeners.
- `openOrder/orderStatus/openOrderEnd/execDetails/execDetailsEnd/commissionReport` `tws_client.py:256` — general listeners (`on_open_order`, `on_order_status`, etc.).

## IBKRExecutionAdapter — `backend/app/oms/ibkr_adapter.py:43`

### Constructor `ibkr_adapter.py:46`

```python
IBKRExecutionAdapter(client=None, host="127.0.0.1", port=7497, client_id=1,
                     timeout=10.0, sec_type="STK", exchange="SMART",
                     currency="USD", submit_pacer=None)
```

Creates `TWSClient()` if `client is None`. Stores `submit_pacer: OrderSubmitPacer | None`. Registers self as `client.register_listener(self)` (`ibkr_adapter.py:90`).

### Fields `ibkr_adapter.py:69`

- `_lock: threading.Lock` — guards all maps.
- `_orders_by_tws_id: dict[int, OMSOrder]` / `_orders_by_internal_id: dict[str, OMSOrder]` / `_tws_id_to_internal_id: dict[int,str]` — bidirectional id maps.
- `_fill_futures: dict[str, tuple[Future[OMSOrder], AbstractEventLoop]]` — waiters for `wait_for_terminal_or_fill`.
- `_state_listeners: list[Any]` — `add_order_state_listener` callbacks `fn(order, kind)`.
- `_exec_id_to_order`, `_seen_exec_ids`, `_commissioned_exec_ids`, `_pending_commissions`, `_partial_qty_emitted`, `_broker_acked`, `_fill_event_emitted` — dedupe/commission correlation.

### Methods

| Method | Location | Purpose |
|--------|----------|---------|
| `is_connected()` | `ibkr_adapter.py:103` | Delegates to `client.is_connected()` |
| `connect()` | `ibkr_adapter.py:107` | Idempotent; `client.connect_and_start(host, port, client_id, timeout)`; raises `ConnectionError` on failure |
| `disconnect()` | `ibkr_adapter.py:131` | `client.disconnect_clean()` |
| `_get_next_tws_order_id()` | `ibkr_adapter.py:136` | Under `_lock`: reads `client.next_order_id` (defaults `1`), increments, returns reserved id |
| `_build_ibkr_contract(order)` | `ibkr_adapter.py:146` | Requires `order.resolved` (set by `instruments/resolver.py`); else `InstrumentResolutionError`. Calls `ibkr_contract_from_resolved(resolved)` |
| `_build_ibkr_order(order)` | `ibkr_adapter.py:155` | Maps `OMSOrder` → `ibapi.order.Order`: `BUY/SELL`, `LMT` (requires `limit_price`) or `MKT`, `transmit=True`, `account=order.intent.ibkr_account` if set |
| `submit_order(order)` | `ibkr_adapter.py:179` | Pacer → dedupe check → reserve TWS id → build contract/order → register maps → `register_request_id(tws_id, "order")` → `client.placeOrder` |
| `adopt_order(order)` | `ibkr_adapter.py:246` | Re-registers existing `OMSOrder` in maps (recovery path) |
| `fetch_broker_order_snapshot()` | `ibkr_adapter.py:255` | `reqOpenOrders()` + `reqExecutions(9003, ExecutionFilter())`; returns `False` if not connected |
| `cancel_order(order_or_id)` | `ibkr_adapter.py:275` | Validates not terminal, `client.cancelOrder(tws_id)` |
| `wait_for_terminal_or_fill(internal_order_id, timeout=10.0)` | `ibkr_adapter.py:307` | Creates `Future` under lock, `asyncio.wait_for`; on timeout pops future and returns current order state; `call_soon_threadsafe` resolution via `_notify_future_if_terminal` |

### Order submission flow

1. `OrderSubmitPacer.acquire()` if pacer set (`ibkr_adapter.py:187`) — `0.2s` min interval, logs delay.
2. Duplicate guard: `internal_order_id` not in `_orders_by_internal_id` (`ibkr_adapter.py:190`).
3. `_get_next_tws_order_id()` reserves `next_order_id` (`ibkr_adapter.py:195`).
4. `_build_ibkr_contract` + `_build_ibkr_order` (`ibkr_adapter.py:198`).
5. Register maps under lock before `placeOrder` to avoid race with sync mock callbacks (`ibkr_adapter.py:203`).
6. `placeOrder(tws_id, contract, ib_order)` (`ibkr_adapter.py:230`); timestamps `ibkr_submit_started_at/completed_at`.

### EWrapper callbacks

| Callback | Location | Key logic |
|----------|----------|-----------|
| `on_order_status` | `ibkr_adapter.py:417` | Looks up by `orderId`, timestamps `order_status_received_at`, maps status via `_map_ib_status`, updates `filled/remaining`, `average_fill_price`/`last_fill_price` via `_usable_price`, calls `_apply_mapped_status`, `_notify_future_if_terminal`, emits via `_callback_event_kinds` |
| `on_open_order` | `ibkr_adapter.py:521` | Ignores unknown `orderId` (no duplicate `OMSOrder` creation). Maps `orderState.status` |
| `on_exec_details` | `ibkr_adapter.py:567` | Resolves `tws_order_id` from `execution.orderId` else `reqId`. Deduplicates `execId`, updates `filled_quantity/remaining_quantity`, creates `BrokerExecution` if new with `price/shares > 0`, correlates pending commissions, weighted-average price |
| `on_exec_details_end` | `ibkr_adapter.py:666` | Debug log only |
| `on_commission_report` | `ibkr_adapter.py:670` | By `execId`; if already commissioned — no-op; if order/execution not yet seen — buffers in `_pending_commissions`; else attaches `commission/currency/realized_pnl` to `BrokerExecution` and emits `COMMISSION` |
| `on_error` | `ibkr_adapter.py:728` | Uses `client.get_request_type(reqId)` to match order. If order terminal — no-op. Code `202` → `CANCELLED`. `_ORDER_REJECTION_CODES {200,201,10147,10148,10243}` → `REJECTED` (checked before warning range). `_NON_TERMINAL_WARNING_CODES {399,2109}` or `2000-2999` or `10000-10999` → warning/info, no state change (prevents false compensation). Else → `ERROR` |
| `on_connection_closed` | `ibkr_adapter.py:778` | Marks all non-terminal orders `ERROR` with `"Connection closed unexpectedly"` and resolves waiters |

### Helpers

- `_map_ib_status(ib_status)` `ibkr_adapter.py:348` — upper + strip spaces: `PENDINGSUBMIT/PRESUBMITTED/APIPENDING`→`PENDING`, `SUBMITTED/PENDINGCANCEL`→`SUBMITTED`, `PARTIALLYFILLED`→`PARTIALLY_FILLED`, `FILLED`→`FILLED`, `CANCELLED/APICANCELLED`→`CANCELLED`, `INACTIVE/REJECTED`→`REJECTED`, else `PENDING`.
- `_apply_mapped_status` `ibkr_adapter.py:365` — no-op if already terminal. Updates filled/remaining, handles terminal statuses, `PARTIALLY_FILLED` when `filled>0 and remaining>0`, `FILLED` when `filled>=quantity`.
- `_notify_future_if_terminal` `ibkr_adapter.py:403` — pops `Future` and `loop.call_soon_threadsafe(fut.set_result, order)` if terminal.
- `_callback_event_kinds` `ibkr_adapter.py:493` + `_maybe_broker_ack` `ibkr_adapter.py:486` — first callback per order emits `BROKER_ACK`; `FILLED` emits `FILL` once; `PARTIALLY_FILLED` emits `PARTIAL_FILL` on new `execDetails` or on `orderStatus` fill qty increase.
- `_usable_price` `ibkr_adapter.py:32` — filters `DBL_MAX`/non-finite/≤0 or `>=1e12`; returns fallback or `None`.
- `_emit_order_state` `ibkr_adapter.py:96` — iterates snapshot of `_state_listeners`, logs on exception.

### Threading model

- IBKR callbacks arrive on `TWSClientThread` (daemon). Adapter guards maps with `threading.Lock` (`ibkr_adapter.py:69`). Async waiters use `asyncio.Future` + `loop.call_soon_threadsafe` (`ibkr_adapter.py:415`) to cross threads safely.

### Reconnect

- `connect()` is idempotent (`ibkr_adapter.py:109`). `TWSClient.connect_and_start` (`tws_client.py:485`) returns `True` immediately if `is_connected()`. `disconnect_clean()` clears thread/event/next_order_id. `connectionClosed` (`tws_client.py:122`) clears `_connected_event` and fans out. Adapter's `on_connection_closed` (`ibkr_adapter.py:778`) fails all working orders to `ERROR`.

### Position retrieval

`TWSClient.request_positions` (`tws_client.py:407`) — see above. Adapter does not directly hold positions; `OrderManager` / recovery uses broker snapshot via `fetch_broker_order_snapshot` + position collector.

### Market data

`TWSClient.tickPrice/tickSize/marketDataType/rerouteMktDataReq` (`tws_client.py:140`) forward to `_market_data_listeners`. Adapter does not subscribe to market data; PnL uses DB `live_pnl` (see streaming doc). `ibkr_market_data_*` settings configure the optional live feed type/symbol but are not required for order execution.

### Error handling

- `submit_order` raises `ConnectionError` if not connected, `ValueError` on duplicate `internal_order_id` or unsupported `order_type` or missing `limit_price`.
- `cancel_order` raises `ConnectionError` if disconnected, `ValueError` if not found / terminal / no `ibkr_order_id`.
- `on_error` classification prevents warnings (`399` order-held, `2109` outside-RTH, all `2xxx`/`10xxx`) from triggering `REJECTED` → compensation.

## Scheduler — `backend/app/broker/ibkr/scheduler.py` — TESTS ONLY

> **Not used in production.** Production pacing is `OrderSubmitPacer(0.2s)` (`backend/app/oms/submit_pacer.py:12`) — one lock, one socket, `min_interval_sec=0.2` (`submit_pacer.py:15`).

`IBKRExecutionScheduler` (`scheduler.py:27`) is a token-bucket gatekeeper for tests:

- Budgets: `DEFAULT_GLOBAL_APP_BUDGET=30 msg/s` (`scheduler.py:15`), `DEFAULT_NORMAL_WORKLOAD_BUDGET=24`, `DEFAULT_EMERGENCY_RESERVE_BUDGET=6`. Ceiling `50 msg/s` (`scheduler.py:12`) is documented IBKR Error 100 limit.
- Priorities (`scheduler.py:19`): `0 EMERGENCY_FLATTEN`, `1 ORDER_EXECUTION`, `2 CONTRACT_DETAILS`, `3 MARKET_DATA`, `4 DIAGNOSTIC`.
- Mechanics: `_global_tokens` + `_normal_tokens` replenished by elapsed time (`scheduler.py:64`). Emergency (P0) consumes only global; others consume both. Throttled wait `max(0.005, 1/rate)` (`scheduler.py:93`). Concurrency via `asyncio.Semaphore(max_concurrent=10)` + per-priority `asyncio.Lock` (`scheduler.py:43`/`51`). `execute_paced(func, *args, priority, request_type)` (`scheduler.py:96`) optionally `await`s coroutine or `to_thread`s sync function, updates `metrics`.

Do not instantiate `IBKRExecutionScheduler` in production code paths.

## Instruments — `backend/app/instruments/*`

### Models `instruments/models.py:12`

- `InstrumentRecord(symbol, sec_type, trade_conid, market_data_conid, exchange, currency, multiplier, underlying_exchange, size_increment)` — master row.
- `ResolvedInstrument(symbol, requested_instrument_type, sec_type, exchange, currency, con_id, market_data_con_id, multiplier, primary_exchange, size_increment)` + `identity_key()`.

### Resolver `instruments/resolver.py:140`

`resolve_leg(symbol, instrument_type, market, currency, con_id, catalog, apply_demo_override)`:

- `ibkr_sec_type` (`resolver.py:111`) — `STK→STK`, `ETF→STK`, `CFD→CFD`; else `InstrumentResolutionError`.
- Temporary paper override: `paper_execute_stk_as_cfd_enabled()` (`instruments/execution_override.py:16` reads `Settings.paper_execute_stk_as_cfd` default `True`) + `execution_instrument_type` (`execution_override.py:22`) maps requested `STK→CFD` as `STK_TO_CFD_DEMO`. When active, requires resolved `sec_type==CFD`; never falls back `CFD→STK`.
- Demo CFD path (`_resolve_demo_cfd` `resolver.py:282`): symbol + `CFD` + `SMART/USD` defaults, no catalog/conId required.
- Non-demo CFD (`_resolve_cfd` `resolver.py:312`): requires `InstrumentRecord` with `sec_type==CFD` and positive `trade_conid`; raises `INSTRUMENT_METADATA_MISSING` otherwise.
- STK (`_resolve_stk` `resolver.py:245`): `market/currency` from signal win over master, fallback `SMART/USD`.
- `attach_resolved(intent, catalog)` (`resolver.py:374`) resolves every `OrderIntent` leg, applies `size_increment` quantization via `apply_size_increment` (`resolver.py:47` round down to whole lots).

### CFD discovery `instruments/cfd_discover.py`

- `cfd_search_contract(symbol, exchange, currency)` (`cfd_discover.py:23`) — `symbol/CFD/SMART/USD` probe contract.
- `pick_unique_cfd_details(details)` (`cfd_discover.py:40`) — filters `secType==CFD` + `currency USD`, prefers `SMART`, returns only if exactly one match else `None`.
- `instrument_record_from_details(details)` (`cfd_discover.py:69`) — maps `ContractDetails` → `InstrumentRecord`, skips `conId<=0`.
- `discover_and_upsert_cfd(symbol, client, session_factory)` (`cfd_discover.py:111`) — `client.request_contract_details_async` (or `to_thread`), `pick_unique`, `InstrumentRepository.upsert`. Best-effort; logs warning on no/ambiguous match or disconnected client.
- `ensure_cfd_instruments_for_symbols(symbols, client, session_factory, catalog)` (`cfd_discover.py:178`) — iterates symbols, skips if catalog already has `CFD` row, else discovers.

### Paper CFD catalog `instruments/paper_cfd_catalog.py:11`

Verified paper rows (discovered against paper Gateway):

- `SIL` CFD `384919303` / `GDX` CFD `134771127`, both `SMART/USD`, `multiplier=1`, `underlying_exchange=ARCA`, `size_increment=1.0`.

## Sequence — Order submission + fill

```mermaid
sequenceDiagram
    participant ENG as OrderManager
    participant ADP as Adapter
    participant GW as Gateway
    ENG->>ADP: submit_order (pacer 0.2s)
    ADP->>GW: placeOrder
    GW-->>ADP: ACK / SUBMITTED
    GW-->>ADP: execDetails + commission
    GW-->>ADP: FILLED
    ADP-->>ENG: result
```

Terminal states: `FILLED / CANCELLED / REJECTED / ERROR` (`ibkr_adapter.py:341`). Warnings `399/2109` and `2xxx/10xxx` do not transition to terminal.

---

## 8. Redis / Streaming (Demo SSE)

> **Source file:** `docs/integrations/streaming.md`  —  original heading: *Demo Streaming — Redis + SSE*

> Source: `backend/demo_streaming/stream.py:8`, `backend/demo_streaming/publisher.py:90`, `backend/demo_streaming/api.py:44`, `backend/demo_streaming/snapshot.py`, `backend/demo_streaming/config.py`, `backend/demo_streaming/main.py`, `frontend/src/hooks/usePnlStream.ts:39`

## Invariant

**Only `demo_streaming` uses Redis.** The main trading app (`app.main`) does not connect to Redis. The demo process is read-only against Postgres and publishes diffs to Redis Streams for the dashboard.

## Redis Streams

### PositionStream — `backend/demo_streaming/stream.py:13`

```python
PositionStream(redis, stream_name="positions:stream", stream_maxlen=10000)
```

| Method | Location | Behavior |
|--------|----------|----------|
| `ping()` | `stream.py:25` | `redis.ping()` |
| `xadd(payload)` | `stream.py:28` | JSON-encodes every field (`_encode` `stream.py:71`), `XADD stream * maxlen ~10000` (`stream.py:32`), returns entry id |
| `xread(last_id="$", block_ms=5000, count=50)` | `stream.py:37` | `XREAD {stream: last_id} block block_ms count count`, handles `RedisTimeout`, decodes bytes + JSON (`_decode_field` `stream.py:75`) |
| `listen(last_id="$", block_ms=5000)` | `stream.py:61` | Async iterator: loops `xread`, advances cursor, yields `(entry_id, fields)` |

Constants: `STREAM_NAME="positions:stream"` (`stream.py:10`), `stream_maxlen=10000` from `DemoStreamSettings.demo_stream_maxlen` (`demo_streaming/config.py:21`).

## PositionBridge — `backend/demo_streaming/publisher.py:33`

Polls Postgres, diffs fingerprints, `XADD`s only on change. Never mutates trading state.

```python
PositionBridge(session_factory, stream, poll_interval=2.0,
               signal_watch_limit=500, pnl_emit_interval=5.0)
```

State (`publisher.py:50`): `_structural_fingerprints`, `_pnl_fingerprints`, `_status`, `_last_payload`, `_signal_fingerprints`, `_last_signal_id`, `_last_pnl_emit`, `_baseline_ready`.

| Method | Location | Behavior |
|--------|----------|----------|
| `restore_baseline()` | `publisher.py:59` | Loads `position_leg_payloads` + `load_signals(for_watch=True)` into fingerprint maps without emitting. Prevents re-emit of existing `OPEN` on restart. |
| `poll_once()` | `publisher.py:92` | Core diff loop — returns `list[dict]` emitted. See below. |
| `run_forever()` | `publisher.py:234` | `restore_baseline()` then loop: `poll_once()` → `sleep(max(0.25, poll_interval - elapsed))`. Logs exceptions, respects `CancelledError`. |
| `_collect(session)` | `publisher.py:273` | `load_position_rows` → `load_baskets` → `load_orders` → `position_leg_payloads` per leg |
| `_payloads_for_vanished(keys)` | `publisher.py:247` | Reloads `CLOSED` rows via `load_position_with_account` so `POSITION_CLOSED` carries `realised_pnl` |

### `poll_once` logic `publisher.py:92`

1. `_collect` payloads (one dict per leg: `account_id/trade_id/symbol` key `publisher.py:294`).
2. Load signals (`load_signals` `snapshot.py:359`), maintain `_signal_fingerprints` filtered to watch window, emit `SIGNAL_RECEIVED` when `signal_id > _last_signal_id` or fingerprint changed (`publisher.py:107`), in reverse order.
3. For each leg payload: compute `structural_fingerprint` (excludes `timestamp/unrealized_pnl/market_data_status`) + `pnl_fingerprint` (`snapshot.py:273`/`279`). Skip if both unchanged. If `_baseline_ready` is false — seed fingerprints without emitting.
   - `structural` changed → `classify_event` (`snapshot.py:73`: `POSITION_OPEN` if `None→OPEN`, `POSITION_CLOSED` if `OPEN→CLOSED`, `POSITION_PARTIAL_CLOSE` if `OPEN` + `close_in_progress` + fill changed, else `POSITION_UPDATE`) → `XADD {event, ...payload}`.
   - `pnl` only changed → buffer per `(account_id, trade_id)` picking one leg (lexicographically smallest symbol) (`publisher.py:168`). Throttled by `pnl_emit_interval` (`publisher.py:179`): emit at most one `POSITION_UPDATE` per trade per interval, then update all leg fingerprints for that trade.
4. Vanished `OPEN` keys → `POSITION_CLOSED` event via `_payloads_for_vanished` (overwrites with DB `CLOSED` row so `realized_pnl` is fresh) (`publisher.py:195`).
5. Sets `_baseline_ready=True`.

`_MIN_POLL_SLEEP_SEC=0.25` (`publisher.py:26`) floors sleep.

## Snapshot helpers — `backend/demo_streaming/snapshot.py`

Read-only Postgres queries. No writes.

| Helper | Location | Purpose |
|--------|----------|---------|
| `position_leg_payloads(position, account, baskets, orders, timestamp)` | `snapshot.py:225` | Expands `PositionModel` + `AccountModel` into 1-2 leg dicts (fields: `account_id/ibkr_account/trade_id/symbol/side/quantity/filled_quantity/entry_price/unrealized_pnl/realized_pnl/commission/status/position_state/basket_state/order_status/broker_order_id/close_in_progress` etc.) |
| `_leg_payload` | `snapshot.py:136` | Per-leg mapping; `live_pnl` quantized to cents (`snapshot.py:54`), `market_data_status LIVE` if `live_pnl != 0` else `UNAVAILABLE` |
| `classify_event` | `snapshot.py:73` | Maps `previous_status/current_status/filled_quantity/close_in_progress` → `POSITION_OPEN/CLOSED/PARTIAL_CLOSE/UPDATE` |
| `structural_fingerprint / pnl_fingerprint / fingerprint` | `snapshot.py:267` | JSON-stable fingerprints; structural excludes `timestamp/unrealized_pnl/market_data_status`, pnl is `unrealized_pnl` only |
| `load_position_rows` | `snapshot.py:284` | `risk_state==OPEN` joined with `AccountModel` |
| `load_closed_position_rows` | `snapshot.py:293` | `risk_state==CLOSED` ordered by `closed_at desc limit 100` |
| `load_position_with_account` | `snapshot.py:310` | Any `risk_state` by `(account_id, trade_id)` |
| `load_baskets / load_orders` | `snapshot.py:325` | Grouped by `(account_id, trade_id)` |
| `load_signals` | `snapshot.py:359` | Paginated signals with `reconcile_signal_status`; `for_watch=True` returns unfiltered recent N; canonical statuses `PROCESSING/ACCEPTED/REJECTED/SQUARE-OFF` |

## Demo API — `backend/demo_streaming/api.py:45`

`create_demo_app(session_factory, redis, stream_name, trading_api_url="http://127.0.0.1:8000", shutdown)` (`api.py:44`):

| Route | Location | Behavior |
|-------|----------|----------|
| `GET /health` | `api.py:74` | `stream.ping()` → `{status: ok/degraded, redis, stream, mode: read-only}` |
| `GET /demo/positions` | `api.py:88` | `load_position_rows` (OPEN only) → per-leg payloads, `{positions, market_data_status: UNAVAILABLE}` |
| `GET /demo/closed-positions?account_id=` | `api.py:112` | `load_closed_position_rows` → `{closed_positions}` |
| `GET /demo/signals?limit&page&page_size&status&account_id&ibkr_account` | `api.py:134` | `load_signals(return_dict=True)` → `{signals, page, page_size, total, filtered_total, total_pages, counts}` |
| `GET /demo/market-data-health` | `api.py:158` | Delegates to `app.state.live_pnl_service.get_market_data_health()` if present, else `{active_subscriptions:0, status:NO_LIVE_PNL_SERVICE}` |
| `GET /demo/stream` | `api.py:169` | SSE `text/event-stream` (`api.py:200` headers `no-cache/keep-alive/X-Accel-Buffering:no`). Sends `hello`, then loops `stream.xread(last_id="$", block_ms=2000, count=20)` (`api.py:180`), emits `data: json\n\n` per entry (`_sse` `api.py:261`), `": keepalive\n\n"` on timeout, `stream_error` on exception |
| `ALL /api/v1/{full_path:path}` | `api.py:210` | Proxy to `trading_api_url` via `httpx.AsyncClient(timeout=30s)`, forwards method/headers/body, preserves query, returns upstream response or `502` |
| `GET /,/accounts,/settings,/system-monitor,/account/{path}` | `api.py:243` | SPA fallback: serves `frontend/dist/index.html` if built else `demo_streaming/static/index.html` |

`SSE_BLOCK_MS=2000` (`api.py:34`) matches the poll interval. Static assets mounted at `/assets` from `frontend/dist/assets` if present (`api.py:70`).

Process wiring `backend/demo_streaming/main.py:30`:

- `DemoStreamSettings` → `Redis.from_url(redis_url, socket_timeout=5, socket_connect_timeout=5)` → `PositionStream` → `PositionBridge` → `create_demo_app` → `uvicorn.Server(host, port)` + `bridge.run_forever()` + `_watch_shutdown` tasks (`main.py:75`). Graceful shutdown cancels tasks, `redis.aclose()`, `engine.dispose()`.

## Frontend — `frontend/src/hooks/usePnlStream.ts:39`

`usePnlStream()` (`usePnlStream.ts:39`) global hook mounted in `App.tsx:14`:

1. On mount: `loadSnapshot` (`usePnlStream.ts:7`) → `GET /demo/positions` + `GET /demo/closed-positions` in parallel, clears store `clearActive()`, applies all legs via `usePnlStore.apply`. If `useSignalStore.accountFilter` set, also `fetchSignals` + `fetchTraySignals`.
2. Then `connect()` (`usePnlStream.ts:50`): `setStreamState(CONNECTING)` → `new EventSource("/demo/stream")` (`usePnlStream.ts:57`). On `onopen` → `LIVE`. On `onmessage` (`usePnlStream.ts:64`) JSON parses: `event==SIGNAL_RECEIVED` → `useSignalStore.handleSignalEvent`, else `apply` as `PositionLeg`.
3. On `onerror` (`usePnlStream.ts:77`): `RECONNECTING`, close source, `setTimeout 1000ms` → `loadSnapshot` → `connect()` (snapshot + reconnect loop).
4. Cleanup on unmount: `stopped=true`, clear timer, close source.

Relative URL `/demo/stream` relies on same-origin proxy in dev (`vite` proxies `/demo → :8010`) and same-origin `demo_streaming` proxy for `/api/v1/*`.

## Frontend data flow

```mermaid
flowchart LR
    DB[("Postgres")] --> BR["Bridge poll 2s"]
    BR --> ST["Stream"]
    ST --> REDIS[("Redis")]
    REDIS --> API["Demo :8010\nSSE"]
    API --> FE["Frontend\nEventSource"]
    API -.-> TRD["API :8000"]
```

Poll → diff → XADD is publisher-only; SSE consumers are XREAD; no Redis usage from trading API.

## Config — `backend/demo_streaming/config.py:6`

`DemoStreamSettings(BaseSettings)` (`config.py:6`, `extra="ignore"`):

| Var | Default | Notes |
|-----|---------|-------|
| `database_url` | `postgresql+asyncpg://root:root123@localhost:5433/ibkr_trading` | Same DB as trading app (read-only) |
| `redis_url` | `redis://127.0.0.1:6379/0` | Redis for streams only |
| `demo_stream_host` | `127.0.0.1` | `0.0.0.0` to expose remotely |
| `demo_stream_port` | `8010` | Separate from trading `:8000` |
| `demo_poll_interval_ms` | `2000` | Bridge `poll_interval` (`2.0s`) |
| `demo_signal_watch_limit` | `500` | Max signals scanned per `poll_once` |
| `demo_pnl_emit_interval_ms` | `5000` | Throttle PnL-only `POSITION_UPDATE` per trade (`5.0s`) |
| `demo_stream_maxlen` | `10000` | `XADD maxlen ~` cap |
| `demo_stream_name` | `positions:stream` | Redis stream key |
| `trading_api_url` | `http://127.0.0.1:8000` | Upstream for `/api/v1/*` proxy |

Logging: `setup_logging(level="INFO", filename_prefix="demo")` → `storage/logs/demo-YYYY-MM-DD.log` (`demo_streaming/main.py:32`).

## End-to-end timeline

1. Trading app writes `positions/baskets/orders/signals` (webhook → workers → OMS → IBKR).
2. Every `poll_interval` (`2s`), `PositionBridge.poll_once` diffs fingerprints.
3. On change, `XADD positions:stream * maxlen ~10000 {event, ...payload}`.
4. Dashboard `usePnlStream` holds `EventSource /demo/stream` (XREAD `block 2000ms`), applies events to stores, with snapshot reload on reconnect.

---

# PART IV — DATABASE


---

## 9. Database — Tables, Columns & Migrations

> **Source file:** `docs/database/database.md`  —  original heading: *Database reference*

**Verified from:** `backend/app/db/models/*` (`account.py`, `basket.py`, `broker_position.py`, `event.py`, `execution.py`, `execution_claim.py`, `execution_settings.py`, `instrument.py`, `kill_switch.py`, `order.py`, `position.py`, `signal.py`, `strategy.py`), `backend/app/db/base.py`, `backend/alembic.ini`, `backend/alembic/env.py`, `backend/alembic/versions/*.py` (18 files).

> All column types, PK/FK definitions, indexes, and constraints below are taken verbatim from the SQLAlchemy models and the Alembic migration files. `Numeric(18,4)` vs `Numeric(18,8)` differences are preserved explicitly.

## 1. Migration system

### 1.1 Files

| File | Purpose |
|------|---------|
| `backend/alembic.ini:1` | Single-DB config. `script_location = %(here)s/alembic`, `sqlalchemy.url = postgresql+asyncpg://root:root123@localhost:5433/ibkr_trading` (overridden at runtime). |
| `backend/alembic/env.py:1` | Async env. `get_settings().database_url` → `config.set_main_option("sqlalchemy.url", ...)` at `backend/alembic/env.py:20`. Registers metadata via `from app.db.base import Base` at `backend/alembic/env.py:10` and `from app.db.models import AccountModel` at `backend/alembic/env.py:11`. `target_metadata = Base.metadata` at `backend/alembic/env.py:29`. Offline + online runners use `async_engine_from_config` with `pool.NullPool`. |
| `backend/app/db/base.py:1` | Declarative base `class Base(DeclarativeBase)` — every `*Model` inherits it. |
| `backend/alembic/versions/*.py` | 18 version files forming one linear chain. Head is `f4a8c2d1e903`. |

### 1.2 Revision chain (linear, no branches)

```
d4bd73bb4fde (None) ──► af6ded376ee5 ──► c3e9f1a2b4d6 ──► a8f3c1d2e4b5 ──► b7c4e8a1d902
  ──► e8a2c4d6f901 ──► f1b3c5d7e902 ──► a9c4e6f8b013 ──► b2d8f4a1c903 ──► c8e1a4b7d205
  ──► c9a1b2c3d4e5 ──► d1e2f3a4b5c6 ──► e2f4a6c8d105 ──► f3a5b7d9e206
  ──► a4c7e2f10938 ──► b6d8f0a2c147 ──► e9f2a7b4c610 ──► f4a8c2d1e903 (head)
```

### 1.3 What each migration did

| # | Revision | File | Summary |
|---|----------|------|---------|
| 1 | `d4bd73bb4fde` | `d4bd73bb4fde_initial_foundation.py:13` | **No-op foundation.** `upgrade()`/`downgrade()` are empty (`pass`). Establishes the initial Alembic head so later revisions have a parent. `down_revision = None`. |
| 2 | `af6ded376ee5` | `af6ded376ee5_create_persistent_schema.py:15` | **Initial persistent schema.** Creates 9 tables: `signals`, `accounts`, `strategies`, `allocations`, `per_symbol_limits`, `orders` (+ indexes `ix_orders_account_status`, `ix_orders_signal_id`), `event_log`, `positions` (PK `trade_id` only at this stage), `instruments`. First schema where `orders.quantity`/`fill_qty` and `positions.leg_*_signed_qty` are `Integer`, PnL/marks are `Numeric(18,4)`. |
| 3 | `c3e9f1a2b4d6` | `c3e9f1a2b4d6_add_trade_id_and_closed_at.py:21` | **Model Blue persistence columns + fractional qty.** Adds `signals.trade_id` + `ix_signals_trade_id`, `orders.trade_id`/`internal_order_id` + `ix_orders_trade_id`/`ix_orders_internal_order_id` (unique), converts `orders.quantity`/`fill_qty` and `positions.leg_a/b_signed_qty` from `Integer` → `Numeric(18,4)`, adds `positions.closed_at TIMESTAMPTZ`. |
| 4 | `a8f3c1d2e4b5` | `a8f3c1d2e4b5_account_strategy_routing.py:21` | **Account × Strategy subscriptions + position PK change.** Adds `allocations.enabled Boolean DEFAULT true`, `uq_allocations_account_strategy (account_id, strategy_id)`, `ck_allocations_alloc_pct_range (alloc_pct BETWEEN 0 AND 1)`, `ck_accounts_total_margin_positive (total_margin > 0)`. Replaces `positions` PK from `trade_id` → composite `(account_id, trade_id)` (`pk_positions_account_trade`) and adds `ix_positions_trade_id`. |
| 5 | `b7c4e8a1d902` | `b7c4e8a1d902_basket_atomicity.py:21` | **Basket state + compensation columns.** Creates `baskets` with `uq_baskets_account_trade_action` and `ix_baskets_strategy_state`. Adds `orders.basket_id FK→baskets.id`, `orders.is_compensation Boolean DEFAULT false`, `orders.compensation_of_internal_order_id String`. |
| 6 | `e8a2c4d6f901` | `e8a2c4d6f901_position_instrument_types.py:21` | Adds `positions.leg_a_instrument_type String NOT NULL DEFAULT 'STK'` and `positions.leg_b_instrument_type String nullable`. |
| 7 | `f1b3c5d7e902` | `f1b3c5d7e902_instrument_size_increment.py:21` | Adds `instruments.size_increment Numeric(18,8) nullable`. |
| 8 | `a9c4e6f8b013` | `a9c4e6f8b013_executions_and_fill_precision.py:22` | **Fill-price precision + executions + event idempotency.** Widens `orders.fill_price` `Numeric(18,4)`→`Numeric(18,8)` and `positions.leg_a/b_entry_mark`, `realised_pnl`, `commission`, `live_pnl` `Numeric(18,4)`→`Numeric(18,8)`. Adds `event_log.basket_id FK→baskets.id` and `event_log.idempotency_key String unique` + `ix_event_log_idempotency_key`. Creates `executions` table (`uq_executions_exec_id`, indexes on `internal_order_id`, `order_id`). |
| 9 | `b2d8f4a1c903` | `b2d8f4a1c903_allocation_max_open_positions.py:21` | **Per-allocation position cap.** Adds `allocations.max_open_positions Integer nullable`, backfills from `strategies.max_open_positions` via `UPDATE allocations SET max_open_positions = s.max_open_positions FROM strategies s`, then `ALTER COLUMN NOT NULL`. |
| 10 | `c8e1a4b7d205` | `c8e1a4b7d205_execution_settings.py:20` | **Singleton execution settings.** Creates `execution_settings` with 6 check constraints (`ck_execution_settings_singleton id=1`, `timeout_pos`, `retries_nonneg`, `interval_pos`, `window_pos`, `window_ge_interval`) and seeds row `id=1 (30,3,5,30)`. |
| 11 | `c9a1b2c3d4e5` | `c9a1b2c3d4e5_create_signal_jobs.py:20` | **Durable signal job queue.** Creates `signal_jobs` (`job_id UUID PK DEFAULT gen_random_uuid()`, `uq_signal_jobs_idempotency_key`, indexes `idx_signal_jobs_status_lease (status, lease_expires_at)` and `idx_signal_jobs_strategy_status (strategy_id, status)`). |
| 12 | `d1e2f3a4b5c6` | `d1e2f3a4b5c6_create_kill_switch_operations.py:20` | Creates `kill_switch_operations` (`operation_id UUID PK DEFAULT gen_random_uuid()`, FK `account_id→accounts.id`, indexes on `account_id` and `status`). No `cleared_*` columns yet. |
| 13 | `e2f4a6c8d105` | `e2f4a6c8d105_create_execution_claims.py:19` | **Dedupe barrier.** Creates `execution_claims` with unique index `uq_execution_claims_dedupe_key (dedupe_key)`, index `ix_execution_claims_signal_id`, and composite `ix_execution_claims_state_claimed_at (state, claimed_at)` for stale-claim sweep. |
| 14 | `f3a5b7d9e206` | `f3a5b7d9e206_signal_jobs_trade_id_status_index.py:18` | Adds `ix_signal_jobs_trade_id_status (trade_id, status)` to support `claim_next_jobs` correlated `NOT EXISTS` guard. |
| 15 | `a4c7e2f10938` | `a4c7e2f10938_normalize_strategy_id_keys.py:42` | **Data migration: normalize `strategy_id` casing + re-hash `idempotency_key`.** Reads every `signal_jobs` row ordered by `received_at`, recomputes `SHA256(lower(strategy_id):signal_id:UPPER(action))`, two-phase renames via `migrating:job_id` namespace to avoid unique violations, parks casing-duplicates as `DEAD_LETTER` with suffixed key `hex:dup:job_id`. `downgrade()` is intentionally no-op. |
| 16 | `b6d8f0a2c147` | `b6d8f0a2c147_kill_switch_clear_columns.py:24` | **Durable kill-switch clear audit.** Adds `kill_switch_operations.cleared_at TIMESTAMPTZ nullable` and `cleared_by String nullable`, plus composite `ix_kill_switch_operations_status_account (status, account_id)` for startup hydration. Historical rows stay in old status (safe: they re-arm on restart). |
| 17 | `e9f2a7b4c610` | `e9f2a7b4c610_account_default_symbol_limit.py:19` | Adds `accounts.default_symbol_limit Numeric(18,4) nullable DEFAULT 10000000.0000`. |
| 18 | `f4a8c2d1e903` | `f4a8c2d1e903_broker_positions_reconcile.py:20` | **Broker inventory + reconcile.** Creates `broker_positions` (composite PK `(ibkr_account, con_id)`, FK `account_id→accounts.id nullable`, `uq_broker_positions_account_conid`, index `ix_broker_positions_account_id`) and `position_reconcile_runs` (JSONB `mismatches DEFAULT []`, counters). **Current HEAD.** |

### 1.4 How to run migrations

```bash
cd backend

# Apply all pending migrations to HEAD (f4a8c2d1e903)
.venv/bin/alembic upgrade head

# Upgrade one step
.venv/bin/alembic upgrade +1

# Downgrade one step (not safe on production after a4c7e2f10938)
.venv/bin/alembic downgrade -1

# Show current revision
.venv/bin/alembic current

# Show history
.venv/bin/alembic history

# Generate a new revision after changing models (autogenerate uses Base.metadata)
.venv/bin/alembic revision --autogenerate -m "description"
```

The DB URL comes from `Settings.database_url` via `alembic/env.py:20` (defaults to `postgresql+asyncpg://root:root123@localhost:5433/ibkr_trading` from `alembic.ini:90` and `app/core/config.py`), not from editing `alembic.ini` directly. Requires `pgcrypto` / `gen_random_uuid()` for UUID defaults (used by `signal_jobs.job_id` and `kill_switch_operations.operation_id`).

---

## 2. Tables

Conventions: `BigInteger` = `BIGINT` (64-bit), `Integer` = `INT`, `Numeric(p,s)` = `NUMERIC(p,s)`, `String` = `VARCHAR/TEXT`, `Boolean` = `BOOLEAN`, `DateTime(timezone=True)` = `TIMESTAMPTZ`, `UUID(as_uuid=True)` = `UUID`, `JSONB` = `JSONB`, `Float` = `DOUBLE PRECISION`. `server_default=func.now()` → `now()` in Postgres.

### 2.1 `accounts` — `AccountModel` at `backend/app/db/models/account.py:11`

Account-level configuration and margin control. No gateway host/port/client_id columns.

| Column | Type | Null | Default | Notes |
|--------|------|------|---------|-------|
| `id` | `BigInteger` | NOT NULL | autoincrement | **PK** |
| `name` | `String` | NOT NULL | — | Human label |
| `ibkr_account` | `String` | NOT NULL | — | IBKR account string (e.g. `DU123456`). Not a FK. |
| `total_margin` | `Numeric(18,4)` | NOT NULL | — |  |
| `enabled` | `Boolean` | NOT NULL | `true` |  |
| `default_symbol_limit` | `Numeric(18,4)` | nullable | `10000000.0000` | Added `e9f2a7b4c610`. Fallback for per-symbol limit. |

Constraints / indexes:
- PK `accounts_pkey (id)`
- CK `ck_accounts_total_margin_positive CHECK (total_margin > 0)` at `backend/app/db/models/account.py:26`

Relationships: parent of `per_symbol_limits`, `allocations`, `orders`, `positions`, `baskets`, `broker_positions`, `executions`, `kill_switch_operations` (all via `accounts.id`).

Lifecycle: `enabled` toggles whether RMS treats the account as tradable; otherwise row is static.

---

### 2.2 `per_symbol_limits` — `PerSymbolLimitModel` at `backend/app/db/models/account.py:30`

Per-account per-symbol money exposure limit. Overrides `accounts.default_symbol_limit`.

| Column | Type | Null | Notes |
|--------|------|------|-------|
| `symbol` | `String` | NOT NULL | **PK part 1** |
| `account_id` | `BigInteger` | NOT NULL | **PK part 2**, **FK → accounts.id** |
| `money_limit` | `Numeric(18,4)` | NOT NULL | Max committed notional for symbol on this account |

Constraints:
- PK `per_symbol_limits_pkey (symbol, account_id)`
- FK `per_symbol_limits_account_id_fkey (account_id → accounts.id)`

Relationship to `AccountModel` at `backend/app/db/models/account.py:41`.

---

### 2.3 `instruments` — `InstrumentModel` at `backend/app/db/models/instrument.py:12`

CFD symbol master. Populated by sync jobs / manual seeding.

| Column | Type | Null | Default | Notes |
|--------|------|------|---------|-------|
| `symbol` | `String` | NOT NULL | — | **PK** |
| `sec_type` | `String` | NOT NULL | — | e.g. `CFD`, `STK` (paper STK→CFD mapping lives in app code) |
| `trade_conid` | `BigInteger` | NOT NULL | — | IBKR `conId` used for order submission |
| `market_data_conid` | `BigInteger` | NOT NULL | — | IBKR `conId` used for market data |
| `underlying_exchange` | `String` | NOT NULL | — | |
| `exchange` | `String` | NOT NULL | — | |
| `currency` | `String` | NOT NULL | — | |
| `multiplier` | `Numeric(18,4)` | NOT NULL | `1` | |
| `size_increment` | `Numeric(18,8)` | nullable | — | Added `f1b3c5d7e902`. Minimal lot step. |

No FKs, no secondary indexes.

---

### 2.4 `strategies` — `StrategyModel` at `backend/app/db/models/strategy.py:24`

Strategy configuration and global position cap. `strategy_id` is the business key.

| Column | Type | Null | Default | Notes |
|--------|------|------|---------|-------|
| `id` | `BigInteger` | NOT NULL | autoincrement | **PK** (surrogate) |
| `strategy_id` | `String` | NOT NULL | — | **Unique** business key, FK target for `allocations.strategy_id` |
| `legs` | `Integer` | NOT NULL | — | 1 or 2 |
| `expression` | `String` | NOT NULL | `CFD` | |
| `max_open_positions` | `Integer` | NOT NULL | — | Global cap (per-allocation cap now in `allocations`) |
| `weight_source` | `String` | NOT NULL | — | |
| `target_delta` | `Numeric(18,4)` | nullable | — | |
| `enabled` | `Boolean` | NOT NULL | `true` | |

Constraints:
- `strategies_strategy_id_key UNIQUE (strategy_id)` (from `unique=True` at `backend/app/db/models/strategy.py:30`)

---

### 2.5 `allocations` — `AllocationModel` at `backend/app/db/models/strategy.py:39`

Account × Strategy margin allocation and exit parameters. One row per subscription.

| Column | Type | Null | Default | Notes |
|--------|------|------|---------|-------|
| `id` | `BigInteger` | NOT NULL | autoincrement | **PK** |
| `account_id` | `BigInteger` | NOT NULL | — | **FK → accounts.id** |
| `strategy_id` | `String` | NOT NULL | — | **FK → strategies.strategy_id** |
| `alloc_pct` | `Numeric(18,4)` | NOT NULL | — | Fraction 0..1 of `total_margin` |
| `target` | `Numeric(18,4)` | NOT NULL | — | Take-profit |
| `stop` | `Numeric(18,4)` | NOT NULL | — | Stop-loss |
| `time_limit` | `Integer` | NOT NULL | — | Bars / seconds |
| `max_open_positions` | `Integer` | NOT NULL | — | Added `b2d8f4a1c903`, backfilled from strategies |
| `enabled` | `Boolean` | NOT NULL | `true` | Added `a8f3c1d2e4b5` |

Constraints:
- FK `allocations_account_id_fkey → accounts.id`, `allocations_strategy_id_fkey → strategies.strategy_id`
- UQ `uq_allocations_account_strategy UNIQUE (account_id, strategy_id)` at `backend/app/db/models/strategy.py:62`
- CK `ck_allocations_alloc_pct_range CHECK (alloc_pct >= 0 AND alloc_pct <= 1)` at `backend/app/db/models/strategy.py:65`

Relationships: `allocation.account → AccountModel`, `allocation.strategy → StrategyModel`.

---

### 2.6 `signals` — `SignalModel` at `backend/app/db/models/signal.py:14`

TradingView / external signal inbox. Immutable after ingest except `status`/`reject_reason`/`processed_at`.

| Column | Type | Null | Default | Notes |
|--------|------|------|---------|-------|
| `id` | `BigInteger` | NOT NULL | autoincrement | **PK** |
| `strategy_id` | `String` | NOT NULL | — | Normalized lowercased (since `a4c7e2f10938`) |
| `signal_id` | `String` | NOT NULL | — | Business id from webhook |
| `trade_id` | `String` | nullable | — | Added `c3e9f1a2b4d6`, `index=True` |
| `action` | `String` | NOT NULL | — | `OPEN`/`CLOSE` etc |
| `pair` | `String` | NOT NULL | — | e.g. `RELIANCE_TCS` |
| `side` | `String` | NOT NULL | — | |
| `ref_price_a` | `Numeric(18,4)` | NOT NULL | — | |
| `ref_price_b` | `Numeric(18,4)` | nullable | — | Single-leg → null |
| `raw_payload` | `JSONB` | NOT NULL | — | Original webhook JSON |
| `status` | `String` | NOT NULL | — | `RECEIVED`/`REJECTED`/`PROCESSED` etc (app-managed, no DB CK) |
| `reject_reason` | `String` | nullable | — | |
| `received_at` | `DateTime(timezone=True)` | NOT NULL | `now()` | |
| `processed_at` | `DateTime(timezone=True)` | nullable | — | |

Constraints / indexes:
- UQ `uq_signals_strategy_signal UNIQUE (strategy_id, signal_id)` at `backend/app/db/models/signal.py:39`
- `ix_signals_trade_id` on `trade_id` (from `c3e9f1a2b4d6`)

Relationships: parent of `orders` and `event_log` via `signals.id`.

---

### 2.7 `signal_jobs` — `SignalJobModel` at `backend/app/db/models/signal.py:63`

Durable signal execution queue. Workers claim rows via `SELECT ... FOR UPDATE SKIP LOCKED`. There is **no FK** from `signal_jobs.signal_id` to anything — `signal_id` is a plain business string.

Lifecycle / state fields — `status` at `backend/app/db/models/signal.py:43`:
```
RECEIVED → QUEUED → CLAIMED → PROCESSING → COMPLETED
                              ↘ REJECTED / FAILED / RECOVERY_REQUIRED / DEAD_LETTER
```
Lease fields `worker_id`, `lease_expires_at` are non-null only while `status IN (CLAIMED, PROCESSING)` (`ACTIVE_LEASE_STATUSES` at `backend/app/db/models/signal.py:57`). `CLAIMABLE_STATUSES` at `backend/app/db/models/signal.py:60` is `(QUEUED, RECEIVED)`.

| Column | Type | Null | Default | Notes |
|--------|------|------|---------|-------|
| `job_id` | `UUID` | NOT NULL | `gen_random_uuid()` | **PK** |
| `signal_id` | `String` | NOT NULL | — | Business key, **not a FK** |
| `strategy_id` | `String` | NOT NULL | — | Normalized lowercased |
| `trade_id` | `String` | nullable | — | `index=True`, plus composite `ix_signal_jobs_trade_id_status` |
| `status` | `String` | NOT NULL | `RECEIVED` | `server_default=RECEIVED` |
| `idempotency_key` | `String` | NOT NULL | — | `unique=True`, `SHA256(strategy_id:signal_id:ACTION)` |
| `account_scope` | `String` | nullable | — | |
| `raw_payload` | `JSONB` | NOT NULL | — | |
| `capture_data` | `JSONB` | nullable | — | Disk-capture metadata |
| `correlation_id` | `String` | NOT NULL | — | |
| `attempt_count` | `BigInteger` | NOT NULL | `0` | |
| `max_attempts` | `BigInteger` | NOT NULL | `3` | |
| `worker_id` | `String` | nullable | — | Lease holder |
| `lease_expires_at` | `DateTime(timezone=True)` | nullable | — | |
| `last_error` | `String` | nullable | — | |
| `received_at` | `DateTime(timezone=True)` | NOT NULL | `now()` | |
| `queued_at` | `DateTime(timezone=True)` | nullable | — | |
| `claimed_at` | `DateTime(timezone=True)` | nullable | — | |
| `processing_started_at` | `DateTime(timezone=True)` | nullable | — | |
| `completed_at` | `DateTime(timezone=True)` | nullable | — | |

Indexes:
- `uq_signal_jobs_idempotency_key UNIQUE (idempotency_key)` (model `unique=True` + migration index)
- `ix_signal_jobs_trade_id` on `trade_id`
- `idx_signal_jobs_status_lease (status, lease_expires_at)` at `c9a1b2c3d4e5`
- `idx_signal_jobs_strategy_status (strategy_id, status)`
- `ix_signal_jobs_trade_id_status (trade_id, status)` at `f3a5b7d9e206`

---

### 2.8 `orders` — `OrderModel` at `backend/app/db/models/order.py:26`

Order ledger and handoff to IBKR. One row per leg.

| Column | Type | Null | Default | Notes |
|--------|------|------|---------|-------|
| `id` | `BigInteger` | NOT NULL | autoincrement | **PK** |
| `signal_id` | `BigInteger` | NOT NULL | — | **FK → signals.id**, `index=True` |
| `trade_id` | `String` | nullable | — | `index=True`, added `c3e9f1a2b4d6` |
| `internal_order_id` | `String` | nullable | — | **Unique**, `index=True`, added `c3e9f1a2b4d6` |
| `basket_id` | `BigInteger` | nullable | — | **FK → baskets.id**, added `b7c4e8a1d902` |
| `is_compensation` | `Boolean` | NOT NULL | `false` | Added `b7c4e8a1d902` |
| `compensation_of_internal_order_id` | `String` | nullable | — | Added `b7c4e8a1d902` |
| `account_id` | `BigInteger` | NOT NULL | — | **FK → accounts.id** |
| `strategy_id` | `String` | NOT NULL | — | Denormalized |
| `leg` | `String` | NOT NULL | — | e.g. `A`/`B` |
| `symbol` | `String` | NOT NULL | — | |
| `ibkr_contract` | `String` | NOT NULL | — | Serialized contract JSON / description |
| `buy_sell` | `String` | NOT NULL | — | `BUY`/`SELL` |
| `quantity` | `Numeric(18,4)` | NOT NULL | — | Was `Integer` until `c3e9f1a2b4d6` |
| `limit_price` | `Numeric(18,4)` | NOT NULL | — | |
| `status` | `String` | NOT NULL | — | e.g. `SENT`/`FILLED`/`CANCELLED`/`REJECTED` |
| `broker_order_id` | `String` | nullable | — | IBKR `orderId` |
| `fill_price` | `Numeric(18,8)` | nullable | — | Wid `a9c4e6f8b013` from `Numeric(18,4)` |
| `fill_qty` | `Numeric(18,4)` | nullable | — | Was `Integer` until `c3e9f1a2b4d6` |
| `filled_at` | `DateTime(timezone=True)` | nullable | — | |
| `margin_impact` | `Numeric(18,4)` | nullable | — | |
| `created_at` | `DateTime(timezone=True)` | NOT NULL | `now()` | |
| `updated_at` | `DateTime(timezone=True)` | NOT NULL | `now()` | `onupdate=now()` |

Indexes / constraints:
- FKs: `orders_signal_id_fkey`, `orders_account_id_fkey`, `fk_orders_basket_id`
- `ix_orders_signal_id (signal_id)`, `ix_orders_trade_id (trade_id)`, `ix_orders_internal_order_id UNIQUE (internal_order_id)`
- `ix_orders_account_status (account_id, status)` at `backend/app/db/models/order.py:76`

Relationships: `order.signal → SignalModel`, `order.account → AccountModel`.

Lifecycle: `status` transitions are app-managed; `fill_*` populated on IBKR fills; `basket_id` links legs of one atomic basket.

---

### 2.9 `executions` — `ExecutionModel` at `backend/app/db/models/execution.py:25`

One IBKR execution (fill leg). Identity is broker `execId` or synthetic fallback.

| Column | Type | Null | Default | Notes |
|--------|------|------|---------|-------|
| `id` | `BigInteger` | NOT NULL | autoincrement | **PK** |
| `exec_id` | `String` | NOT NULL | — | **Unique** `uq_executions_exec_id` |
| `order_id` | `BigInteger` | nullable | — | **FK → orders.id**, `index=True` |
| `account_id` | `BigInteger` | nullable | — | **FK → accounts.id** |
| `internal_order_id` | `String` | NOT NULL | — | `index=True`, joins back to `orders` |
| `broker_order_id` | `String` | nullable | — | |
| `symbol` | `String` | NOT NULL | — | |
| `side` | `String` | NOT NULL | — | `BOT`/`SLD` |
| `quantity` | `Numeric(18,8)` | NOT NULL | — | |
| `price` | `Numeric(18,8)` | NOT NULL | — | |
| `commission` | `Numeric(18,8)` | nullable | — | |
| `commission_currency` | `String` | nullable | — | |
| `realized_pnl` | `Numeric(18,8)` | nullable | — | |
| `perm_id` | `BigInteger` | nullable | — | IBKR permId |
| `executed_at` | `DateTime(timezone=True)` | nullable | — | Broker timestamp |
| `created_at` | `DateTime(timezone=True)` | NOT NULL | `now()` | |
| `updated_at` | `DateTime(timezone=True)` | NOT NULL | `now()` | `onupdate=now()` |

Indexes: `uq_executions_exec_id`, `ix_executions_internal_order_id`, `ix_executions_order_id`. Added `a9c4e6f8b013`. Relationships: `execution.order → OrderModel`, `execution.account → AccountModel`.

---

### 2.10 `baskets` — `BasketModel` at `backend/app/db/models/basket.py:23`

Account-scoped multi-leg basket execution state. Not a position ledger.

| Column | Type | Null | Default | Notes |
|--------|------|------|---------|-------|
| `id` | `BigInteger` | NOT NULL | autoincrement | **PK** |
| `account_id` | `BigInteger` | NOT NULL | — | **FK → accounts.id** |
| `trade_id` | `String` | NOT NULL | — | Business trade identifier |
| `strategy_id` | `String` | NOT NULL | — | |
| `action` | `String` | NOT NULL | — | `OPEN`/`CLOSE` (part of UQ) |
| `state` | `String` | NOT NULL | — | `PENDING`/`FILLED`/`CRITICAL` etc |
| `intended_leg_count` | `Integer` | NOT NULL | — | Expected number of legs |
| `created_at` | `DateTime(timezone=True)` | NOT NULL | `now()` | |
| `updated_at` | `DateTime(timezone=True)` | NOT NULL | `now()` | `onupdate=now()` |

Constraints / indexes:
- FK `baskets_account_id_fkey → accounts.id`
- UQ `uq_baskets_account_trade_action UNIQUE (account_id, trade_id, action)` at `backend/app/db/models/basket.py:50`
- `ix_baskets_strategy_state (strategy_id, state)` from `b7c4e8a1d902`

Relationship: `basket.account → AccountModel`; parent of `orders.basket_id`.

---

### 2.11 `positions` — `PositionModel` at `backend/app/db/models/position.py:16`

Pair-level position ledger. One row = one logical pair position. **Composite PK `(account_id, trade_id)`**.

| Column | Type | Null | Default | Notes |
|--------|------|------|---------|-------|
| `account_id` | `BigInteger` | NOT NULL | — | **PK part 1**, **FK → accounts.id** |
| `trade_id` | `String` | NOT NULL | — | **PK part 2** |
| `strategy_id` | `String` | NOT NULL | — | |
| `leg_a_symbol` | `String` | NOT NULL | — | |
| `leg_a_signed_qty` | `Numeric(18,4)` | NOT NULL | — | Was `Integer` until `c3e9f1a2b4d6` |
| `leg_a_entry_mark` | `Numeric(18,8)` | NOT NULL | — | Was `Numeric(18,4)` until `a9c4e6f8b013` |
| `leg_b_symbol` | `String` | nullable | — | Single-leg → null |
| `leg_b_signed_qty` | `Numeric(18,4)` | nullable | — | Was `Integer` until `c3e9f1a2b4d6` |
| `leg_b_entry_mark` | `Numeric(18,8)` | nullable | — | Was `Numeric(18,4)` until `a9c4e6f8b013` |
| `realised_pnl` | `Numeric(18,8)` | NOT NULL | `0` | Was `Numeric(18,4)` until `a9c4e6f8b013` |
| `commission` | `Numeric(18,8)` | NOT NULL | `0` | |
| `live_pnl` | `Numeric(18,8)` | NOT NULL | `0` | Coalesced from `LivePnlService`, not tick-by-tick |
| `target` | `Numeric(18,4)` | NOT NULL | — | |
| `stop` | `Numeric(18,4)` | NOT NULL | — | |
| `time_limit` | `Integer` | NOT NULL | — | |
| `leg_a_instrument_type` | `String` | NOT NULL | `STK` | Added `e8a2c4d6f901` |
| `leg_b_instrument_type` | `String` | nullable | — | Added `e8a2c4d6f901` |
| `opened_at` | `DateTime(timezone=True)` | NOT NULL | `now()` | |
| `closed_at` | `DateTime(timezone=True)` | nullable | — | Added `c3e9f1a2b4d6` |
| `risk_state` | `String` | NOT NULL | — | e.g. `OPEN`/`CLOSED`/`STOPPED` |

Constraints / indexes:
- PK `pk_positions_account_trade (account_id, trade_id)` (changed `a8f3c1d2e4b5` from single `trade_id`)
- FK `positions_account_id_fkey → accounts.id`
- `ix_positions_trade_id (trade_id)` from `a8f3c1d2e4b5`

Relationship: `position.account → AccountModel`.

Lifecycle: `opened_at` on insert; `closed_at` set on close; `realised_pnl`/`commission` finalized at close; `live_pnl` updated periodically.

---

### 2.12 `event_log` — `EventLogModel` at `backend/app/db/models/event.py:17`

Append-only system event audit trail. Never updated.

| Column | Type | Null | Default | Notes |
|--------|------|------|---------|-------|
| `id` | `BigInteger` | NOT NULL | autoincrement | **PK** |
| `ts` | `DateTime(timezone=True)` | NOT NULL | `now()` | |
| `process` | `String` | NOT NULL | — | e.g. worker / OMS / RMS |
| `signal_id` | `BigInteger` | nullable | — | **FK → signals.id** |
| `order_id` | `BigInteger` | nullable | — | **FK → orders.id** |
| `basket_id` | `BigInteger` | nullable | — | **FK → baskets.id**, added `a9c4e6f8b013` |
| `kind` | `String` | NOT NULL | — | Event type string |
| `idempotency_key` | `String` | nullable | — | **Unique**, added `a9c4e6f8b013`, `ix_event_log_idempotency_key` |
| `detail` | `JSONB` | NOT NULL | — | Arbitrary payload |

FKs / indexes:
- `event_log_signal_id_fkey`, `event_log_order_id_fkey`, `fk_event_log_basket_id`
- `ix_event_log_idempotency_key UNIQUE (idempotency_key)`

Relationships: `event.signal → SignalModel`, `event.order → OrderModel`.

---

### 2.13 `broker_positions` — `BrokerPositionModel` at `backend/app/db/models/broker_position.py:26`

Latest IBKR position inventory snapshot. Fully replaced each 30 s reconcile sweep by `PositionReconciler`.

| Column | Type | Null | Default | Notes |
|--------|------|------|---------|-------|
| `ibkr_account` | `String` | NOT NULL | — | **PK part 1** |
| `con_id` | `BigInteger` | NOT NULL | — | **PK part 2** (IBKR `conId`) |
| `account_id` | `BigInteger` | nullable | — | **FK → accounts.id**, `index=True` |
| `symbol` | `String` | NOT NULL | — | |
| `sec_type` | `String` | NOT NULL | — | |
| `currency` | `String` | NOT NULL | — | |
| `exchange` | `String` | NOT NULL | `""` | `server_default=""` |
| `signed_qty` | `Numeric(18,4)` | NOT NULL | — | Signed position |
| `avg_cost` | `Numeric(18,8)` | NOT NULL | — | IBKR avg cost |
| `as_of` | `DateTime(timezone=True)` | NOT NULL | `now()` | Snapshot time |

Constraints:
- PK `broker_positions_pkey (ibkr_account, con_id)`
- UQ `uq_broker_positions_account_conid UNIQUE (ibkr_account, con_id)` (redundant with PK, kept for ORM `UniqueConstraint` at `backend/app/db/models/broker_position.py:49`)
- FK `broker_positions_account_id_fkey → accounts.id`
- `ix_broker_positions_account_id (account_id)`

Relationship: `broker_position.account → AccountModel | None` (unmapped accounts have `account_id IS NULL`).

---

### 2.14 `position_reconcile_runs` — `PositionReconcileRunModel` at `backend/app/db/models/broker_position.py:53`

One row per periodic broker-vs-ledger reconcile sweep. Summary + mismatches JSON.

| Column | Type | Null | Default | Notes |
|--------|------|------|---------|-------|
| `id` | `BigInteger` | NOT NULL | autoincrement | **PK** |
| `started_at` | `DateTime(timezone=True)` | NOT NULL | `now()` | |
| `finished_at` | `DateTime(timezone=True)` | nullable | — | |
| `timed_out` | `Boolean` | NOT NULL | `false` | |
| `error` | `String` | nullable | — | |
| `broker_line_count` | `BigInteger` | NOT NULL | `0` | |
| `match_count` | `BigInteger` | NOT NULL | `0` | |
| `ghost_count` | `BigInteger` | NOT NULL | `0` | Broker position with no ledger entry |
| `orphan_count` | `BigInteger` | NOT NULL | `0` | Ledger position with no broker position |
| `drift_count` | `BigInteger` | NOT NULL | `0` | Qty mismatch |
| `unmapped_account_count` | `BigInteger` | NOT NULL | `0` | Lines for unknown IBKR account |
| `mismatches` | `JSONB` | NOT NULL | `[]` | `list[dict]` detail, `server_default='[]'` |

No FKs. Created `f4a8c2d1e903`.

---

### 2.15 `execution_claims` — `ExecutionClaimModel` at `backend/app/db/models/execution_claim.py:22`

Durable execution dedupe barrier. A row is `CLAIMED` before broker submit and promoted to `EXECUTED` afterwards. Unique `dedupe_key` is the enforcement; `RMSContext.processed_signals` in-memory set is not authoritative.

State at `backend/app/db/models/execution_claim.py:17`: `CLAIMED` → `EXECUTED` / `ABANDONED`.

| Column | Type | Null | Default | Notes |
|--------|------|------|---------|-------|
| `id` | `BigInteger` | NOT NULL | autoincrement | **PK** |
| `dedupe_key` | `String` | NOT NULL | — | **Unique** indexed `uq_execution_claims_dedupe_key` |
| `account_id` | `BigInteger` | nullable | — | **No FK** (plain int) |
| `strategy_id` | `String` | NOT NULL | — | |
| `signal_id` | `String` | NOT NULL | — | `index=True` (`ix_execution_claims_signal_id`) |
| `action` | `String` | NOT NULL | — | `OPEN`/`CLOSE` |
| `state` | `String` | NOT NULL | `CLAIMED` | `server_default=CLAIMED` |
| `attempt_count` | `BigInteger` | NOT NULL | `1` | `server_default=1` |
| `correlation_id` | `String` | nullable | — | |
| `last_note` | `String` | nullable | — | |
| `claimed_at` | `DateTime(timezone=True)` | NOT NULL | `now()` | |
| `executed_at` | `DateTime(timezone=True)` | nullable | — | Set on `EXECUTED` |

Indexes: `uq_execution_claims_dedupe_key UNIQUE (dedupe_key)`, `ix_execution_claims_signal_id`, `ix_execution_claims_state_claimed_at (state, claimed_at)` at `e2f4a6c8d105`.

No ORM relationship is declared.

---

### 2.16 `execution_settings` — `ExecutionSettingsModel` at `backend/app/db/models/execution_settings.py:9`

Singleton dashboard config for basket fill timeout and incomplete-leg retries. Exactly one row `id=1`.

| Column | Type | Null | Default | Notes |
|--------|------|------|---------|-------|
| `id` | `BigInteger` | NOT NULL | — | **PK**, CK `id = 1` |
| `enabled` | `Boolean` | NOT NULL | `true` | |
| `square_off_after_sec` | `Integer` | NOT NULL | `30` | Fill timeout |
| `max_retries` | `Integer` | NOT NULL | `3` | |
| `retry_interval_sec` | `Integer` | NOT NULL | `5` | |
| `retry_window_sec` | `Integer` | NOT NULL | `30` | |

Check constraints at `backend/app/db/models/execution_settings.py:21`:
- `ck_execution_settings_singleton CHECK (id = 1)`
- `ck_execution_settings_timeout_pos CHECK (square_off_after_sec > 0)`
- `ck_execution_settings_retries_nonneg CHECK (max_retries >= 0)`
- `ck_execution_settings_interval_pos CHECK (retry_interval_sec > 0)`
- `ck_execution_settings_window_pos CHECK (retry_window_sec > 0)`
- `ck_execution_settings_window_ge_interval CHECK (retry_window_sec >= retry_interval_sec)`

Seeded `INSERT (1, true, 30, 3, 5, 30)` in `c8e1a4b7d205`.

---

### 2.17 `kill_switch_operations` — `KillSwitchOperationModel` at `backend/app/db/models/kill_switch.py:26`

Durable Emergency Flatten / kill-switch operations. Activation blocks new `OPEN`s until operator explicitly clears; completing the flatten is not the same as clearing (`CLEARED` is a separate terminal state that disarms).

Status constants at `backend/app/db/models/kill_switch.py:13`:
```
IDLE → ACTIVATING → FLATTENING → RECONCILING → RETRYING → FLAT → COMPLETE → UNRESOLVED
                                                    ↘ CLEARED (terminal, disarmed)
```

| Column | Type | Null | Default | Notes |
|--------|------|------|---------|-------|
| `operation_id` | `UUID` | NOT NULL | `uuid4()` / `gen_random_uuid()` | **PK** |
| `account_id` | `BigInteger` | NOT NULL | — | **FK → accounts.id**, `index=True` |
| `ibkr_account` | `String` | NOT NULL | — | Denormalized |
| `status` | `String` | NOT NULL | `ACTIVATING` | `index=True` |
| `requested_by` | `String` | NOT NULL | `operator` | |
| `initial_position_count` | `BigInteger` | NOT NULL | `0` | |
| `flattened_count` | `BigInteger` | NOT NULL | `0` | |
| `working_count` | `BigInteger` | NOT NULL | `0` | |
| `retrying_count` | `BigInteger` | NOT NULL | `0` | |
| `unresolved_count` | `BigInteger` | NOT NULL | `0` | |
| `final_exposure` | `Float` | NOT NULL | `0.0` | `DOUBLE PRECISION` |
| `last_error` | `String` | nullable | — | |
| `cleared_at` | `DateTime(timezone=True)` | nullable | — | Added `b6d8f0a2c147` |
| `cleared_by` | `String` | nullable | — | Added `b6d8f0a2c147` |
| `created_at` | `DateTime(timezone=True)` | NOT NULL | `now()` / `datetime.now(UTC)` | Python default + `server_default=now()` |
| `updated_at` | `DateTime(timezone=True)` | NOT NULL | `now()` | `onupdate=datetime.now(UTC)` + `server_default=now()` |

Indexes:
- `ix_kill_switch_operations_account_id (account_id)`
- `ix_kill_switch_operations_status (status)`
- `ix_kill_switch_operations_status_account (status, account_id)` added `b6d8f0a2c147`

No ORM relationship beyond FK.

---

## 3. Cross-table notes

- There is **no** `signal_legs` child table; legs live in `signals.pair`/`raw_payload`, `orders.leg`, and `positions.leg_a/b_*`.
- There are **no** `gateways`, `gateway_clients`, or `account_gateway_bindings` tables (target-only design at `docs/backend-multi-gateway.md`).
- `signal_jobs.signal_id` and `execution_claims.signal_id` are plain `String` business keys, **not FKs** — their uniqueness is enforced via `idempotency_key` / `dedupe_key` hashing, not referential integrity.
- `positions` uses a **composite PK** `(account_id, trade_id)`; the lone `trade_id` index is secondary. Always qualify by `account_id`.
- `broker_positions` PK and UQ are identical `(ibkr_account, con_id)`; `account_id` is nullable for unmapped IBKR accounts.
- `event_log` is append-only; `idempotency_key` unique prevents duplicate audit writes.
- Monetary columns: `Numeric(18,4)` for notionals/limits/quantities/stops/targets; `Numeric(18,8)` for fill prices/entry marks/PnL/commission/avg_cost and execution quantities/prices (after `a9c4e6f8b013`).
- UUID PKs (`signal_jobs.job_id`, `kill_switch_operations.operation_id`) require `pgcrypto` for `gen_random_uuid()` default; Python side also sets `uuid4()`.
- `alembic_version` table (managed by Alembic) holds the single current head `f4a8c2d1e903`; not defined in ORM.

---

## 10. ER Diagram

> **Source file:** `docs/database/er-diagram.md`  —  original heading: *ER Diagram*

**Verified from:** `backend/app/db/models/*` — FK via `ForeignKey()`. Full column list in `database.md`.

```mermaid
erDiagram
    accounts {
        BigInt id PK
        String ibkr_account
        Numeric total_margin
        Bool enabled
    }
    strategies {
        BigInt id PK
        String strategy_id UK
        Int max_open_positions
    }
    allocations {
        BigInt id PK
        BigInt account_id FK
        String strategy_id FK
        Numeric alloc_pct
    }
    signals {
        BigInt id PK
        String strategy_id
        String signal_id
        String trade_id
        String status
    }
    signal_jobs {
        UUID job_id PK
        String status
        String idempotency_key UK
    }
    baskets {
        BigInt id PK
        BigInt account_id FK
        String trade_id
        String state
    }
    orders {
        BigInt id PK
        BigInt signal_id FK
        BigInt account_id FK
        String internal_order_id UK
        String status
    }
    executions {
        BigInt id PK
        String exec_id UK
        BigInt order_id FK
    }
    positions {
        BigInt account_id PK_FK
        String trade_id PK
        String risk_state
    }
    event_log {
        BigInt id PK
        String process
        String kind
    }
    broker_positions {
        String ibkr_account PK
        BigInt con_id PK
        Numeric signed_qty
    }
    kill_switch_operations {
        UUID operation_id PK
        BigInt account_id FK
        String status
    }
    per_symbol_limits {
        String symbol PK
        BigInt account_id PK_FK
        Numeric money_limit
    }
    instruments {
        String symbol PK
        BigInt trade_conid
    }

    accounts ||--o{ allocations : ""
    strategies ||--o{ allocations : ""
    accounts ||--o{ per_symbol_limits : ""
    signals ||--o{ orders : ""
    accounts ||--o{ orders : ""
    baskets ||--o{ orders : ""
    accounts ||--o{ baskets : ""
    accounts ||--o{ positions : ""
    orders ||--o{ executions : ""
    accounts ||--o{ kill_switch_operations : ""
    accounts ||--o{ broker_positions : ""
    signals ||--o{ event_log : ""
    orders ||--o{ event_log : ""
    baskets ||--o{ event_log : ""
```

> Full columns, types, constraints and 18 migrations → `database.md`. Isolated tables (no FK): `instruments`, `signal_jobs`, `execution_claims`, `execution_settings`, `position_reconcile_runs`.

### Key

- Solid lines = `ForeignKey()` in models. Isolated tables have no lines.
- Composite PKs: `positions(account_id,trade_id)`, `broker_positions(ibkr_account,con_id)`, `per_symbol_limits(symbol,account_id)`.

---

# PART V — REFERENCE


---

## 11. API Reference

> **Source file:** `docs/reference/api.md`  —  original heading: *API Reference — Code-Accurate*

> Inspected from `backend/app/main.py:145-175`, `backend/app/api/router.py:1-18`, `backend/app/api/routes/*.py`, `backend/demo_streaming/api.py:1-262`. No endpoints are invented. Paths include their router prefixes as mounted.

## Mounting Summary

```python
# backend/app/main.py:168-170
app.include_router(health_router)                          # no prefix
app.include_router(webhooks_router, prefix="/api")         # → /api/webhooks/*
app.include_router(api_router, prefix="/api/v1")           # → /api/v1/*
    # api_router includes: orders, config, emergency, system_monitor, reconcile
```

| Router file | `APIRouter` prefix | Included under | Effective prefix |
|---|---|---|---|
| `routes/health.py:5` | `` (none) | `app` directly | `/health` |
| `routes/webhooks.py:24` | `/webhooks` | `/api` | `/api/webhooks` |
| `routes/orders.py:13` | `` (none) | `/api/v1` | `/api/v1/orders` |
| `routes/config.py:43` | `/config` | `/api/v1` | `/api/v1/config` |
| `routes/emergency.py:25` | `` (none) | `/api/v1` | `/api/v1/emergency-kill-switch` |
| `routes/reconcile.py:18` | `/reconcile` | `/api/v1` | `/api/v1/reconcile` |
| `routes/system_monitor.py:12` | `/system-monitor` | `/api/v1` | `/api/v1/system-monitor` |
| `demo_streaming/api.py:45-258` | standalone `FastAPI` on `:8010` | — | `/demo/*`, `/health` (demo) |

Main trading app (`app.main:app`) has **no** CORS, no WebSocket, no static mount. Global exception handler (`main.py:157-165`) returns `500 {"detail":"Internal server error..."}` for unhandled exceptions.

---

## 1. Health

### `GET /health` — Main App Health
- **File:** `backend/app/api/routes/health.py:8-11`
- **Purpose:** Liveness probe for the trading API.
- **Auth:** None.
- **Request:** No body, no params.
- **Response:** `200 OK` `{"status":"ok"}` (`dict[str, str]`).
- **Status codes:** `200` always (unless 500 handler).
- **Side effects:** None. Does not touch DB, TWS, Redis.
- **Can place/cancel/modify orders:** No.

### `GET /health` — Demo Stream Health (port 8010)
- **File:** `backend/demo_streaming/api.py:74-86`
- **Purpose:** Demo SSE process liveness + Redis reachability.
- **Auth:** None.
- **Response:** `200` `{"status":"ok"|"degraded","redis":bool,"stream":str,"mode":"read-only"}`.
- **Side effects:** `await stream.ping()` → `Redis.ping()`.
- **Can trade:** No (read-only process, no TWS).

---

## 2. Webhooks — TradingView Ingestion

### `POST /api/webhooks/tradingview`
- **File:** `backend/app/api/routes/webhooks.py:166-304`
- **Purpose:** Fast, durable ingestion of TradingView alerts into Postgres queue `signal_jobs`. Workers process asynchronously.
- **Auth:** Optional `X-Webhook-Secret` header. Checked **before** any DB access (`_verify_webhook_authentication` at `webhooks.py:144-161`):
  - If `WEBHOOK_AUTH_ENABLED=false` → auth skipped (log info).
  - If `WEBHOOK_AUTH_SECRET` is set → `hmac.compare_digest(expected, incoming)` constant-time compare. Missing/invalid → `401 Unauthorized` (`detail: "Unauthorized: Missing or invalid authentication secret."`).
  - If `WEBHOOK_AUTH_ENABLED=true` but secret is empty string, same 401 path is taken when header doesn't match.
- **Request body:** Raw `application/json` bytes (`await request.body()` at line 178). Parsed via `json.loads`. Must be a JSON object (dict). Fields inspected by `compute_idempotency_key` (`backend/app/services/worker_pool.py:27-47`):
  - `strategy` or `strategy_id` → normalized via `normalize_strategy_id`
  - `trade_id` or `signal_id` → normalized via `normalize_trade_id`
  - `action` → uppercased, used to derive `signal_id` (`{trade_id}:CLOSE` for CLOSE)
  - Any additional payload is preserved verbatim in `raw_payload` and `capture_data`.
- **Processing:**
  1. Assign `request_id = uuid4()` (`webhooks.py:203`), bind log context.
  2. `compute_idempotency_key(payload)` → `(strategy_id, signal_id, trade_id, idempotency_key=sha256("{strategy_id}:{signal_id}:{action}"))`.
  3. Resolve `session_factory` from `request.app.state.session_factory` (fallback to `order_manager._session_factory`).
  4. `SignalJobRepository.create_job_if_not_exists(...)` (atomic `INSERT ... ON CONFLICT DO NOTHING` on `idempotency_key`):
     - First time → row inserted with `status=QUEUED`, `queued_at`, `received_at`, `capture_data={metadata:{request_id,received_at},raw_body,parsed_json}`.
     - Duplicate `idempotency_key` → existing job returned, `created=False`, logged as duplicate.
  5. Writes disk capture: `data/tradingview_webhooks/{timestamp}.json` via `_save_raw_capture_file` (off-loop helper) and appends CSV row to `data/tradingview_webhooks/incoming_signals.csv` via `_append_incoming_signal_csv` (thread-locked, `asyncio.to_thread`).
- **Response:** `202 Accepted` `TradingViewWebhookResponse` (`backend/app/schemas/webhook.py:6-13`):
  ```json
  {"status":"accepted","source":"tradingview","signal_id":"<derived>","job_id":"<uuid>","request_id":"<uuid>"}
  ```
  Duplicate still returns `202` with same `job_id`/`signal_id`.
- **Status codes:**
  - `202` accepted (including duplicate)
  - `400` malformed JSON / non-dict payload (`detail: "Invalid or malformed JSON..."`)
  - `401` bad/missing webhook secret
  - `500` no session factory or DB persist failure (`detail: "Failed to durably persist signal job."`)
- **DB side effects:** Insert into `signal_jobs` (idempotent). Disk JSON + CSV side files.
- **Trading side effects:** None synchronously. **Does not place orders.** The 10-worker pool (`ExecutionWorkerPool`) claims and executes later via `OrderManager.process_signal_execution` → RMS → basket OMS → IBKR.
- **Note:** TradingView retry on 5xx will dedupe via same `idempotency_key`.

---

## 3. Orders — OMS Read / Cancel

All routes use dependency `get_oms` (`backend/app/api/deps.py`) → `request.app.state.oms` (`OMSService`). Read-only except `DELETE`.

### `GET /api/v1/orders`
- **File:** `backend/app/api/routes/orders.py:16-26`
- **Purpose:** List all tracked internal orders from in-memory `OMSService._orders`.
- **Auth:** None.
- **Request:** No params/body.
- **Response:** `200` `list[OrderSchema]` (`backend/app/schemas/api_schemas.py:12-26`): `order_id`, `symbol`, `side` (BUY/SELL), `quantity`, `order_type`, `status` (`OMSOrderStatus`), `timestamp`, `price`, `filled_quantity`, `average_fill_price`.
- **Status:** `200` always.
- **Side effects:** None (reads `oms.get_all_orders()`).
- **Can place/cancel/modify:** No (read-only).

### `GET /api/v1/orders/{order_id}`
- **File:** `backend/app/api/routes/orders.py:29-42`
- **Purpose:** Fetch one internal order by `internal_order_id` (e.g., `ORD-<account>-<signal>-L0`).
- **Auth:** None.
- **Params:** `order_id: str` path.
- **Response:** `200` `OrderSchema` if found; `404 {"detail":"Order {id} not found."}` otherwise.
- **Side effects:** None (`oms.get_order`).
- **Can trade:** No.

### `DELETE /api/v1/orders/{order_id}`
- **File:** `backend/app/api/routes/orders.py:45-68`
- **Purpose:** Cancel an open order through the `OMSService` → `IBKRExecutionAdapter.cancel_order` → `TWSClient.cancelOrder(tws_order_id)`.
- **Auth:** None.
- **Params:** `order_id: str` path.
- **Response:** `200` `OrderSchema` of cancelled order on success.
- **Status codes:**
  - `200` cancellation submitted (order status transitions asynchronously via callbacks)
  - `404` order ID not found (`"not found"` in error)
  - `400` terminal state / no IBKR ID / TWS not connected (`ValueError`/`ConnectionError` mapped to 400)
- **DB/trading side effects:** Calls `adapter.cancelOrder`. Does not mutate DB directly; broker callbacks update `OMSOrder.status` and event log later.
- **Can place/cancel/modify orders:** **Yes — cancels** orders. Does not place new orders.

---

## 4. Config — Dashboard CRUD (`/api/v1/config/*`)

All routes use `get_db_session` (`backend/app/db/session.py`) + `AccountStrategyConfigService`. Mutations call `session.commit()` / `rollback()` and many trigger `order_manager.reload_rms_limits()` or `reload_execution_policy()` to hot-reload in-memory RMS state.

### `GET /api/v1/config/accounts`
- **File:** `backend/app/api/routes/config.py:50-102`
- **Purpose:** List every account with nested allocations + per-symbol limits + kill-switch arm flag (for Settings UI).
- **Auth:** None.
- **Response:** `200` `AccountsConfigResponse {accounts: list[AccountConfigSchema]}`. Each account includes `id`, `name`, `ibkr_account`, `total_margin`, `enabled`, `default_symbol_limit`, `kill_switch_active` (`is_account_kill_switch_active`), `allocations`, `symbol_limits`.
- **DB:** Reads `AccountModel`, `AllocationModel`, `PerSymbolLimitModel`.
- **Can trade:** No.

### `GET /api/v1/config/accounts/by-identifier/{ibkr_account}`
- **File:** `backend/app/api/routes/config.py:105-148`
- **Purpose:** Lookup one account config by IBKR account string (case-insensitive `func.upper`).
- **Params:** `ibkr_account: str` path (stripped + uppercased).
- **Response:** `200` `AccountConfigSchema`; `404` if not found.
- **Can trade:** No.

### `POST /api/v1/config/accounts`
- **File:** `backend/app/api/routes/config.py:287-328`
- **Purpose:** Create a new paper trading account.
- **Auth:** None.
- **Request body:** `CreateAccountRequest` (`config_schemas.py:54-61`): `name: str (min1)`, `ibkr_account: str (min1)` (stored uppercased), `total_margin: Decimal (>0)`, `enabled: bool = true`, `default_symbol_limit: Decimal (>0) | None`.
- **Response:** `201` `AccountConfigSchema` (empty allocations/limits). Validation via `AccountStrategyConfigService.create_account`.
- **Status codes:** `201` created; `400` `AllocationConfigError` (duplicate IBKR, invalid margin, etc.); `422` Pydantic validation.
- **DB:** Inserts `AccountModel`.
- **Can trade:** No.

### `PATCH /api/v1/config/accounts/{account_id}`
- **File:** `backend/app/api/routes/config.py:331-402`
- **Purpose:** Partial update of account mutable fields.
- **Params:** `account_id: int` path.
- **Request body:** `PatchAccountRequest` (`config_schemas.py:64-71`): any of `name`, `ibkr_account`, `total_margin`, `enabled`, `default_symbol_limit` (must provide at least one, else `400 "No fields to update."`).
- **Response:** `200` `AccountConfigSchema` with refreshed allocations/limits + `kill_switch_active`.
- **Status:** `404` account not found; `400` validation (e.g., `IBKR account cannot be changed when history exists`); `200` on success.
- **Side effects:** On success, `await order_manager.reload_rms_limits()` to refresh `RMSContext.per_symbol_limits` / `default_symbol_limits`.
- **Can trade:** No.

### `GET /api/v1/config/accounts/{account_id}/deletable`
- **File:** `backend/app/api/routes/config.py:445-464`
- **Purpose:** Check whether account can be safely deleted (no orders/executions/positions/baskets).
- **Params:** `account_id: int`.
- **Response:** `200` `AccountDeleteCheckResponse {can_delete: bool, reason: str|None, has_history: bool}`. `check_account_deletable` + `has_trading_history`.
- **Status:** `404` if account missing.
- **Can trade:** No.

### `DELETE /api/v1/config/accounts/{account_id}`
- **File:** `backend/app/api/routes/config.py:467-497`
- **Purpose:** Safely delete an account with no trading history; cascades allocations, per-symbol limits, kill-switch ops.
- **Params:** `account_id: int`.
- **Response:** `204 No Content` on success (no body).
- **Status:** `404` not found; `400` `AllocationConfigError` if `has_trading_history` is true.
- **Side effects:** Deletes `AccountModel` + related rows; `order_manager.reload_rms_limits()`; discards from `_KILL_SWITCH_ACTIVE_ACCOUNTS`.
- **Can trade:** No (refuses if history exists).

### `POST /api/v1/config/accounts/{account_id}/allocations`
- **File:** `backend/app/api/routes/config.py:405-442`
- **Purpose:** Assign a strategy allocation (capital pct) to an account.
- **Params:** `account_id: int`.
- **Request body:** `CreateAllocationRequest` (`config_schemas.py:74-84`): `strategy_id: str`, `alloc_pct: Decimal [0,1]`, `max_open_positions: int|None (≥0)`, `target: Decimal (>0) default 500`, `stop: Decimal (>0) default 250`, `time_limit: int (>0) default 3600`, `enabled: bool = true`.
- **Response:** `201` `AllocationConfigSchema`.
- **Status:** `404` account missing; `400` duplicate strategy, unknown strategy, enabled sum >1.0, invalid alloc_pct, etc.
- **DB:** Inserts `AllocationModel`.
- **Can trade:** No.

### `PATCH /api/v1/config/allocations/{allocation_id}`
- **File:** `backend/app/api/routes/config.py:499-537`
- **Purpose:** Update `alloc_pct`, `enabled`, `max_open_positions` on an existing allocation.
- **Params:** `allocation_id: int`.
- **Request body:** `PatchAllocationRequest` (`config_schemas.py:94-99`): at least one field else `400`.
- **Response:** `200` `AllocationConfigSchema`.
- **Status:** `404` not found; `400` validation / sum exceeded.
- **Can trade:** No.

### `PUT /api/v1/config/accounts/{account_id}/symbol-limits/{symbol}`
- **File:** `backend/app/api/routes/config.py:540-570`
- **Purpose:** Upsert per-symbol money limit (RMS check 8 budget).
- **Params:** `account_id: int`, `symbol: str` path (uppercased).
- **Request body:** `PutSymbolLimitRequest` (`config_schemas.py:102-105`): `money_limit: Decimal (>0)`.
- **Response:** `200` `SymbolLimitSchema {symbol, money_limit}`.
- **Side effects:** `order_manager.reload_rms_limits()` after commit.
- **Status:** `400` on validation; `404` account not found propagated as 400 via `AllocationConfigError`.
- **Can trade:** No (but changes RMS gate for future orders).

### `PUT /api/v1/config/accounts/{account_id}/default-symbol-limit`
- **File:** `backend/app/api/routes/config.py:573-623`
- **Purpose:** Update the account-wide fallback per-symbol limit.
- **Request body:** `PutDefaultSymbolLimitRequest` (`config_schemas.py:108-111`): `default_symbol_limit: Decimal (>0)`.
- **Response:** `200` `AccountConfigSchema` (full account with refreshed lists).
- **Side effects:** `reload_rms_limits`.
- **Status:** `404` account missing; `400` validation.
- **Can trade:** No.

### `DELETE /api/v1/config/accounts/{account_id}/symbol-limits/{symbol}`
- **File:** `backend/app/api/routes/config.py:626-646`
- **Purpose:** Remove a per-symbol limit (falls back to `default_symbol_limit`).
- **Params:** `account_id: int`, `symbol: str`.
- **Response:** `204 No Content`.
- **Side effects:** `reload_rms_limits`.
- **Status:** `404` if limit not found.
- **Can trade:** No.

### `GET /api/v1/config/execution`
- **File:** `backend/app/api/routes/config.py:660-672`
- **Purpose:** Read paper auto square-off / incomplete-leg retry settings (`execution_settings` table id=1).
- **Response:** `200` `ExecutionSettingsSchema` (`config_schemas.py:114-124`): `enabled`, `square_off_after_sec`, `max_retries`, `retry_interval_sec`, `retry_window_sec`, `paper_retries_active` (computed: `paper_retry_ports_allowed(settings.ibkr_port) and row.enabled`).
- **DB:** `get_or_create_execution_settings` (creates default row if missing: `enabled=true, 30, 3, 5, 30`).
- **Can trade:** No.

### `PATCH /api/v1/config/execution`
- **File:** `backend/app/api/routes/config.py:675-716`
- **Purpose:** Update retry policy fields.
- **Request body:** `PatchExecutionSettingsRequest` (`config_schemas.py:127-134`): any of `enabled`, `square_off_after_sec (>0)`, `max_retries (≥0)`, `retry_interval_sec (>0)`, `retry_window_sec (>0)`; at least one else `400`.
- **Response:** `200` `ExecutionSettingsSchema` (with recomputed `paper_retries_active`).
- **Side effects:** `order_manager.reload_execution_policy()` → `BasketCoordinator.apply_retry_policy` (validates + sets `fill_timeout`, `paper_retries_allowed`).
- **Status:** `400` no fields / `AllocationConfigError` on validation.
- **Can trade:** No (but changes basket retry behavior).

---

## 5. Config-Adjacent Trading Controls (still under `/api/v1/config`)

### `POST /api/v1/config/accounts/{account_id}/square-off` — Kill-Switch Square-Off
- **File:** `backend/app/api/routes/config.py:151-191`
- **Purpose:** Emergency: arm kill switch for one account and **flatten all OPEN positions** via broker.
- **Auth:** None (operator endpoint; UI calls it same-origin via demo proxy).
- **Params:** `account_id: int`.
- **Request:** No body.
- **Response:** `202 Accepted` `SquareOffResponse` (`config_schemas.py:137-145`): `account_id`, `ibkr_account`, `squared_off_count` (= `op.initial_position_count`), `trade_ids: []`, `operation_id: str(uuid)`, `status: str` (e.g., `ACTIVATING`).
- **Status:** `404` account missing; `202` even if an active operation already exists (idempotent, returns existing `op`).
- **DB/trading side effects:** **Yes — places orders.**
  1. `KillSwitchService(session_factory, order_manager).initiate_square_off(account_id, "operator")` → inserts `KillSwitchOperationModel` (`ACTIVATING`), arms `_KILL_SWITCH_ACTIVE_ACCOUNTS`, counts OPEN positions.
  2. If `created_new` → `execute_flatten_operation_background(op.operation_id)` (non-blocking `asyncio.create_task`). The HTTP response returns immediately; flatten runs asynchronously (bounded concurrency 5, each leg as `MARKET` with `EMERGENCY_FLATTEN` intent, then reconciliation).
  3. Staying armed: the operation is **not** auto-cleared; new `OPEN` intents for this `account_id` are blocked until `POST .../kill-switch/clear`.
- **Can place/cancel/modify orders:** **Yes — places MARKET close orders** for every OPEN position (via `BasketCoordinator`).

### `POST /api/v1/config/accounts/{account_id}/kill-switch/clear`
- **File:** `backend/app/api/routes/config.py:194-234`
- **Purpose:** Disarm the durable kill-switch so new OPENs resume.
- **Auth:** None.
- **Params:** `account_id: int`.
- **Response:** `200` `KillSwitchClearResponse {account_id, ibkr_account, operations_cleared: int, kill_switch_active: bool}`.
- **Status:** `404` account missing; `200` always otherwise (even if nothing was armed).
- **Side effects:** `clear_account_kill_switch(session_factory, account_id, "operator")` → `UPDATE kill_switch_operations SET status=CLEARED WHERE status IN _ARMED_STATUSES`; removes `account_id` from `_KILL_SWITCH_ACTIVE_ACCOUNTS`. Completing a flatten does **not** disarm — this is the only way to re-enable OPENs.
- **Can trade:** No (but unblocks future OPENs).

### `GET /api/v1/config/accounts/{account_id}/kill-switch`
- **File:** `backend/app/api/routes/config.py:237-256`
- **Purpose:** Report whether account is blocked from opening new positions.
- **Params:** `account_id: int`.
- **Response:** `200` `KillSwitchStatusResponse {account_id, kill_switch_active: bool}` (`is_account_kill_switch_active`).
- **Status:** `404` if account missing.
- **Can trade:** No.

### `POST /api/v1/config/accounts/{account_id}/positions/{trade_id}/close`
- **File:** `backend/app/api/routes/config.py:259-284`
- **Purpose:** Close **only** the selected open pair (trade_id) without arming global kill switch.
- **Auth:** None.
- **Params:** `account_id: int`, `trade_id: str`.
- **Response:** `200` `ClosePairResponse {account_id, ibkr_account, trade_id, leg_a_symbol, leg_b_symbol|None, status, success, message|None}`.
- **Side effects:** `SinglePairCloseService(session_factory, order_manager).close_pair(...)` → validates OPEN position exists, builds `CLOSE` intent with reversed legs, submits via `BasketCoordinator` (MARKET). Does **not** mutate kill-switch state.
- **Can place/cancel/modify orders:** **Yes — places MARKET close orders** for the selected pair.

---

## 6. Emergency Kill Switch — External Webhook

### `POST /api/v1/emergency-kill-switch` (prompt shorthand: `/emergency-kill-switch`)
- **File:** `backend/app/api/routes/emergency.py:76-148`
- **Purpose:** External pre-flight trigger (e.g., off-EC2) that **arms** the durable account kill-switch state on EC2 **without** executing broker flatten. EC2-side flatten is already handled by `square-off`; this endpoint only flips the block.
- **Auth:** Bearer token via `Authorization: Bearer <secret>` (`_verify_emergency_killswitch_auth` at `emergency.py:28-73`):
  - If `EMERGENCY_KILLSWITCH_AUTH_ENABLED=false` → auth skipped.
  - If enabled and `EMERGENCY_KILLSWITCH_AUTH_SECRET` empty → `401 "Emergency kill switch authentication not configured."` (fail-closed).
  - Missing header → `401 "Missing Authorization header."`
  - Malformed (not `Bearer <token>`) → `401 "Malformed Authorization header. Expected Bearer token."`
  - `hmac.compare_digest` mismatch → `401 "Invalid emergency authentication secret."` (never logs secret).
  - Checked **before** any DB mutation.
- **Request body:** `EmergencyKillSwitchRequest` (`config_schemas.py:177-180`): `ibkr_account_id: str (min1)` — the IBKR account string (e.g., `DUR919062`).
- **Response:** `200` `EmergencyKillSwitchResponse {success: true, ibkr_account_id: str, kill_switch_active: true, message: str}` where `message` is `"Emergency kill switch activated..."` if newly armed or `"Kill switch was already active..."` if idempotent.
- **Status codes:**
  - `200` armed (or already armed)
  - `400` `ibkr_account_id` empty after trim
  - `401` auth failure
  - `404` IBKR account not found (`Account with IBKR identifier '...' not found`)
  - `500` DB persist failure
- **DB side effects:** `KillSwitchService.arm_account_kill_switch_only(account_id, "emergency_webhook")` → inserts or reuses `KillSwitchOperationModel` (`ACTIVATING`), arms `_KILL_SWITCH_ACTIVE_ACCOUNTS`. Idempotent on repeat calls.
- **Trading side effects:** **No broker orders.** This deliberately does **not** call `execute_flatten_operation_background`. EC2 flatten must be triggered separately via `square-off` if desired.
- **Can place/cancel/modify orders:** **No** — arms state only.

---

## 7. Reconcile — Broker vs Ledger Observability

### `GET /api/v1/reconcile/positions`
- **File:** `backend/app/api/routes/reconcile.py:21-39`
- **Purpose:** Read-only view of the last background reconcile sweep: broker snapshot, OPEN Model Blue ledger pairs, and freshly classified diffs. Does **not** call `reqPositions`; reflects the `PositionReconciler`'s last `replace_snapshot` run (every 30s).
- **Auth:** None.
- **Query params:** `ibkr_account: str | None` — optional filter; passed to `collect_reconcile_positions`.
- **Response:** `200` `ReconcilePositionsResponse` (`reconcile_schemas.py:68-74`):
  - `run: ReconcileRunSummary | None` — `id`, `finished_at`, `timed_out`, `error`, `broker_line_count`, `match_count`, `ghost_count`, `orphan_count`, `drift_count`, `unmapped_account_count`
  - `broker_positions: list[BrokerPositionSnapshotRow]` — `ibkr_account`, `con_id`, `account_id|None`, `symbol`, `sec_type`, `currency`, `exchange`, `signed_qty`, `avg_cost`, `as_of`
  - `ledger_positions: list[LedgerPositionRow]` — `account_id`, `ibkr_account|None`, `trade_id`, `strategy_id`, `leg_a_symbol`, `leg_a_signed_qty`, `leg_a_instrument_type`, `leg_b_symbol|None`, `leg_b_signed_qty|None`, `leg_b_instrument_type|None`, `risk_state`
  - `diffs: list[ReconcileDiffRow]` — `kind` (`MATCH`/`LEDGER_GHOST`/`BROKER_ORPHAN`/`QTY_DRIFT`/`UNMAPPED_ACCOUNT`), `ibkr_account|None`, `account_id|None`, `symbol`, `sec_type`, `con_id|None`, `broker_qty|None`, `ledger_qty|None`, `in_flight: bool`
- **Side effects:** None (reads `broker_positions`, `position_reconcile_runs`, `positions`).
- **Can trade:** No.

### `POST /api/v1/reconcile/positions/flatten`
- **File:** `backend/app/api/routes/reconcile.py:42-70`
- **Purpose:** Flatten **one** persisted broker snapshot line (market reverse). Quantity/side come from the snapshot, not the request.
- **Auth:** None.
- **Request body:** `FlattenBrokerPositionRequest` (`reconcile_schemas.py:77-83`): `ibkr_account: str`, `symbol: str`, `sec_type: str`, `con_id: int (>0)` (all trimmed, case-normalized server-side).
- **Response:** `200` `FlattenBrokerPositionResponse` (`reconcile_schemas.py:86-98`): `ibkr_account`, `account_id|None`, `symbol`, `sec_type`, `con_id`, `side` (SELL if long else BUY), `quantity`, `status` (`FLAT`/`PARTIAL`/`FAILED`), `success: bool`, `message|None`.
- **Status codes:**
  - `200` flattened (check `success`/`status`)
  - `400` symbol/sec_type mismatch vs snapshot, zero qty
  - `404` snapshot line not found for `(ibkr_account, con_id)`
  - `503` execution dependency unavailable (`baskets coordinator` missing)
- **DB/trading side effects:** **Yes — places orders.**
  - Look up `BrokerPositionRepository.get_snapshot_line`.
  - Build synthetic `OrderIntent(signal_id=RECON-FLAT-{con_id}-{uuid}, action=CLOSE, strategy_id=reconcile_flatten, legs=[symbol...], intent_mode=EMERGENCY_FLATTEN)` with `quantity=abs(signed_qty)`, `side` reversed.
  - `_resolve_instruments`, `RMSResult PASS`, `baskets_coord.execute(..., order_type=MARKET)`.
  - Dedupe via `_IN_FLIGHT_BROKER_FLATTENS[(ibkr_account, con_id)]` — concurrent duplicate calls await same task.
  - Does **not** arm kill switch or mutate `positions` ledger directly.
- **Can place/cancel/modify orders:** **Yes — places MARKET reverse** for the snapshot line.

---

## 8. System Monitor — Operational Observability

### `GET /api/v1/system-monitor`
- **File:** `backend/app/api/routes/system_monitor.py:15-32`
- **Purpose:** Read-only EC2 observability: host resources + service health + alerts.
- **Auth:** None.
- **Request:** No params/body aside from injected `tws_client`/`redis_client` from `app.state` (for latency probes).
- **Response:** `200` `SystemMonitorResponse` (`backend/app/schemas/system_monitor.py:88-101`):
  - `overall_status: "HEALTHY"|"DEGRADED"|"CRITICAL"`
  - `timestamp: datetime`
  - `system: SystemInfoResponse` — `hostname`, `operating_system`, `os_version`, `kernel_version`, `architecture`, `cpu_count`, `total_memory_bytes`, `system_uptime_seconds`, `load_avg`, `timezone`, `instance_type`
  - `cpu: CpuMetrics` — `usage_percent`, `count`, `load_avg_1m/5m/15m`
  - `memory: MemoryMetrics` — `ram`+`swap` each `MetricUsage {total_bytes, used_bytes, available_bytes, percent}`
  - `storage: list[StorageMetrics]` — `mount`, `filesystem`, `usage: MetricUsage`, `status: OK|WARNING|CRITICAL`
  - `services: ServicesHealth` — `backend`, `demo_stream`, `ib_gateway`, `postgresql`, `redis` each `ServiceStatus {name, status: RUNNING|DEGRADED|STOPPED|UNKNOWN, port, health_detail, latency_ms|None}`
  - `network: dict[str, Any]`
  - `alerts: list[AlertItem {level: INFO|WARNING|CRITICAL, component, message}>`
  - `top_processes: list[ProcessInfo {pid, name, cpu_percent, memory_percent, status}>`
  - (Collects via `collect_system_monitor_data` in `backend/app/services/system_monitor_service.py`.)
- **Side effects:** Reads OS metrics, probes DB/Redis/TWS. No mutations.
- **Can trade:** No.

---

## 9. Demo Stream — Read-Only Dashboard (port 8010, `demo_streaming`)

The demo process (`python -m demo_streaming`, `backend/demo_streaming/main.py:30-96`, served via `uvicorn` on `DEMO_STREAM_HOST:DEMO_STREAM_PORT` default `127.0.0.1:8010`) has its own `FastAPI` app (`demo_streaming/api.py:45`). It **never** connects to TWS or trades. Redis Streams at `positions:stream` carry position diffs published by `PositionBridge`.

| Method & Path | File | Purpose | Auth | Response | Trading |
|---|---|---|---|---|---|
| `GET /health` | `api.py:74` | Demo liveness + Redis | None | `{"status","redis","stream","mode":"read-only"}` | No |
| `GET /demo/positions` | `api.py:88` | OPEN positions (leg rows, with basket/order snapshot) | None | `{"positions":[...],"market_data_status":"UNAVAILABLE"}` | Read-only |
| `GET /demo/closed-positions?account_id=` | `api.py:112` | CLOSED positions leg rows | None | `{"closed_positions":[...]}` | Read-only |
| `GET /demo/signals?limit&page&page_size&status&account_id&ibkr_account` | `api.py:134` | Signals inbox (paginated) | None | `{"signals":[...]}` or `{"signals":...,"total":...}` via `load_signals` | Read-only |
| `GET /demo/market-data-health` | `api.py:158` | Live PnL subscription health (proxies `LivePnlService.get_market_data_health` if available) | None | `{"active_subscriptions","contracts":[...]}` | No |
| `GET /demo/stream` | `api.py:169` | **SSE** `text/event-stream` — Redis `XREAD` of `positions:stream` with `hello` + `keepalive` + `stream_error` events | None | SSE `data: {json}\n\n` (fields + `redis_id`) | No |
| `GET|POST|PATCH|PUT|DELETE /api/v1/{full_path:path}` | `api.py:210` | Reverse proxy to trading API (`TRADING_API_URL` → `http://127.0.0.1:8000`) for same-origin dashboard | Forwards headers (excl. host/content-length/connection), 30s timeout | Upstream response proxied; `502` if trading API unreachable | Depends (forwards trading calls) |
| `GET /`, `/accounts`, `/settings`, `/system-monitor`, `/account/{path}` | `api.py:243` | SPA fallback — serves `frontend/dist/index.html` if built, else `demo_streaming/static/index.html` (`no-store`) | None | HTML | No |
| `GET /assets/*` | `api.py:70` | Vite build assets (`frontend/dist/assets`) | None | Static files | No |
| `GET /favicon.svg` | `api.py:251` | Favicon | None | SVG or 404 | No |

**SSE details** (`api.py:169-208`):
- `last_id="$"` then `XREAD block_ms=SSE_BLOCK_MS(2000) count=20`.
- Disconnect-aware (`request.is_disconnected()`), sends `: keepalive` when no entries.
- On Redis error, emits `{"event":"stream_error","market_data_status":"UNAVAILABLE"}` then sleeps 1s.

**Publisher** (`demo_streaming/publisher.py:33-295` `PositionBridge`): polls Postgres every `DEMO_POLL_INTERVAL_MS/1000` (default 2s), diffs structural vs pnl fingerprints, emits `SIGNAL_RECEIVED`, `BASKET_*`, `POSITION_UPDATE`, `POSITION_CLOSED` via `PositionStream.xadd` (Redis `XADD MAXLEN ~10000`). `demo_streaming/stream.py:13-82` handles `xadd`/`xread` + JSON encode/decode.

---

## Endpoints NOT Present (do not document as existing)

Per `docs/gaps.md` and AGENTS.md warning, the following are **not** implemented on `app.main` despite appearing in stale docs:
- Any CORS / WebSocket / static file serving on the trading API (only on demo `:8010`).
- `BROKER_MODE` / `MockBroker` toggle (ignored via `extra="ignore"` in `Settings`).
- Multi-gateway pool, per-gateway rate limits, reconnect-on-drop (single `TWSClient` + `OrderSubmitPacer` 0.2s).
- Top-level `POSTMAN_API_TESTING_GUIDE.md` endpoints that do not exist.

---

## 12. Class Reference

> **Source file:** `docs/reference/classes.md`  —  original heading: *Classes Reference — Code-Accurate*

> Inspected from `backend/app/**`. Line refs are exact. Methods list caller/callee edges.

---

## 1. TWSClient — `backend/app/broker/ibkr/tws_client.py:16`

**Purpose:** Thin `EWrapper+EClient` adapter bridging the synchronous ibapi callback thread to the asyncio app. Owns the background reader thread and all TWS callback fan-out.

**Responsibilities:**
- TCP connect to TWS/IB Gateway, wait for `nextValidId` handshake, maintain `next_order_id`/`_connected_event`.
- Run `self.run()` in a daemon thread (`TWSClientThread`) so FastAPI event loop is not blocked.
- Dispatch every `EWrapper` callback (`error`, `connectionClosed`, `tickPrice`, `contractDetails*`, `position*`, `openOrder`, `orderStatus`, `execDetails*`, `commissionReport`, …) to two listener lists: `_market_data_listeners` (LivePnL) and `_listeners` (adapter, reconciler).
- Provide synchronous blocking helpers `request_contract_details`, `request_positions` (with `threading.Event` and timeouts) and their `async` wrappers via `asyncio.to_thread`.

**Dependencies:** `ibapi.client.EClient`, `ibapi.wrapper.EWrapper`, `app.broker.ibkr.positions.PositionSnapshotCollector` (for positions sweep).

**Important state (`tws_client.py:24-44`):**
- `next_order_id: int|None` — handshake-supplied order id cursor.
- `_connected_event: threading.Event` — true after `nextValidId`.
- `_thread: Thread|None` — reader thread.
- `_market_data_listeners`, `_listeners: list[Any]` — callback subscribers.
- `_request_types: dict[int,str]` + `_registry_lock` — maps reqId → "order"/"contract_details".
- `_contract_details_events/_results/_next_contract_details_req` — per-request blocking machinery.
- `_positions_request_lock` + `_position_collector`.

**Public methods:**

| Method | Purpose | Inputs | Returns | Side effects | Called by | Calls |
|---|---|---|---|---|---|---|
| `__init__` | Init EWrapper/EClient, zero state | — | — | — | `main.lifespan` | `EWrapper.__init__` |
| `register_request_id / unregister / get_request_type` | Thread-safe reqId registry | `req_id, req_type` | `str|None` | mutates `_request_types` | `IBKRExecutionAdapter.submit_order`, `TWSClient.request_contract_details` | — |
| `nextValidId` | Handshake complete, set cursor | `orderId:int` | — | sets `next_order_id`, `event.set` | ibapi | — |
| `error` | Log + fan-out errors (2000-2999 = info) | `reqId, errorCode, errorString` | — | fan-out to both listener lists | ibapi | `listener.on_error` |
| `connectionClosed` | Mark disconnected | — | — | `clear`, `next_order_id=None`, fan-out `on_connection_closed` | ibapi | — |
| `tickPrice/tickSize/marketDataType/rerouteMktDataReq` | Market data fan-out | `reqId, tickType, price, ...` | — | — | ibapi | `listener.on_tick_price` etc. |
| `contractDetails / contractDetailsEnd` | Accumulate results + signal event | `reqId, contractDetails` | — | appends `_results`, `event.set` | ibapi | `listener.on_contract_details` |
| `register_market_data_listener / register_listener` | Subscribe | `listener:Any` | — | append to list | `LivePnlService.__init__`, `IBKRExecutionAdapter.__init__` | — |
| `request_contract_details` | Block for `reqContractDetails` result | `contract:Any, timeout=5.0` | `list[Any]` | registers reqId, calls `reqContractDetails`, waits `event.wait` | `LivePnlService._request_ticks`, `OrderManager.hydrate_live_pnl` paths, `IBKRExecutionAdapter` indirect | `reqContractDetails`, `_clear_contract_details_request` |
| `request_contract_details_async` | Async wrapper | same | `list[Any]` | `await to_thread` | `async` callers | `request_contract_details` |
| `request_positions` | Block for `reqPositions` snapshot | `timeout=15.0` | `tuple[list[BrokerPositionLine], bool timed_out]` | registers `PositionSnapshotCollector`, `reqPositions`, optional `cancelPositions`, collects `collector.snapshot()` | `PositionReconciler.run_once` (via async) | — |
| `request_positions_async` | Async wrapper | same | same | `to_thread` | `PositionReconciler._persist_and_diff` | `request_positions` |
| `is_connected` | Handshake + socket check | — | `bool` | — | `IBKRExecutionAdapter`, `PositionReconciler`, `LivePnlService` | `EClient.isConnected` |
| `connect_and_start` | TCP connect + start thread + await handshake | `host, port, client_id, timeout=10.0` | `bool` | `connect`, `Thread(run).start`, `event.wait` | `main.lifespan` | `EClient.connect`, `_connected_event.wait` |
| `disconnect_clean` | Disconnect + join thread | — | — | `disconnect`, `join(2s)`, clear events/maps | `main.lifespan` shutdown | `EClient.disconnect` |

---

## 2. IBKRExecutionAdapter — `backend/app/oms/ibkr_adapter.py:43`

**Purpose:** The single OMS → TWS bridge. Serializes `OMSOrder` into `ibapi.contract.Contract` + `ibapi.order.Order`, calls `placeOrder`/`cancelOrder`, and translates broker callbacks (`on_order_status`, `on_open_order`, `on_exec_details`, `on_commission_report`, `on_error`) into `OMSOrder` state transitions.

**Responsibilities:** Resolve `next_order_id`, build contract/order, pacer-gate `submit_order`, maintain `internal_id↔tws_id` maps, deduplicate `execId`, accumulate `BrokerExecution` records + commissions, emit `BROKER_ACK/PARTIAL_FILL/FILL/COMMISSION` via `_state_listeners`, implement `fetch_broker_order_snapshot`, `wait_for_terminal_or_fill`.

**Dependencies:** `TWSClient`, `OrderSubmitPacer`, `app.instruments.resolver.ibkr_contract_from_resolved`, `app.rms.models.OrderSide`.

**Important state (`ibkr_adapter.py:56-88`):**
- `_client`, `_host/_port/_client_id/_timeout/_sec_type/_exchange/_currency`, `_submit_pacer`.
- `_lock: threading.Lock` — guards all maps.
- `_orders_by_tws_id/_orders_by_internal_id/_tws_id_to_internal_id`.
- `_fill_futures: dict[str,(Future,Loop)]` — awaited by `wait_for_terminal_or_fill` & `BasketCoordinator._wait_terminals`.
- `_state_listeners: list[callable(order,kind)]` — subscribed by `BasketCoordinator`.
- `_exec_id_to_order/_broker_acked/_fill_event_emitted/_seen_exec_ids/_commissioned_exec_ids/_pending_commissions/_partial_qty_emitted`.

**Public methods:**

| Method | Purpose | Inputs | Returns/Side effects | Caller → Callee |
|---|---|---|---|---|
| `__init__` | Wire client, config, register listener | `client, host, port, client_id, timeout, sec_type="STK", exchange="SMART", currency="USD", submit_pacer` | — | `main.lifespan` → `client.register_listener(self)` |
| `add_order_state_listener` | Subscribe basket | `listener(order,kind)` | append | `BasketCoordinator.__init__` |
| `_emit_order_state` | Fan-out `kind` | `order, kind` | call listeners | callbacks → listeners |
| `is_connected` | Delegate | — | `bool` | `OMSService`, `TWSClient`, `BasketCoordinator` |
| `connect/disconnect` | Delegate connect | — | — | lifecycle (main does not use `adapter.connect`; it calls `client.connect_and_start` directly) |
| `_get_next_tws_order_id` | Atomic increment cursor | — | `int` | `submit_order` |
| `_build_ibkr_contract` | `resolved → Contract` via resolver | `order:OMSOrder` | `Contract` | `submit_order` → `ibkr_contract_from_resolved` |
| `_build_ibkr_order` | `OMSOrder → IBOrder` (BUY/SELL, MKT/LMT, account) | `order` | `IBOrder` | `submit_order` |
| `submit_order` | Pacer-gate, register maps, `placeOrder` | `order:OMSOrder` | `OMSOrder` (timestamps set) | `OMSService._submit_leg` → `submit_pacer.acquire`, `client.placeOrder` |
| `adopt_order` | Register pre-existing order for callbacks | `order:OMSOrder` | — | recovery/hydrate | — |
| `fetch_broker_order_snapshot` | Fire `reqOpenOrders` + `reqExecutions(9003)` if connected | — | `bool` (false if disconnected) | `BasketCoordinator.recover_incomplete_baskets`, `RecoveryManager.run_startup_recovery` | `client.reqOpenOrders`, `client.reqExecutions` |
| `cancel_order` | `cancelOrder(tws_id)` | `order_or_id: OMSOrder|str` | `OMSOrder` | `OMSService.cancel_order` → `client.cancelOrder` |
| `wait_for_terminal_or_fill` | Async wait for `FILLED/CANCELLED/REJECTED/ERROR` with timeout | `internal_order_id, timeout=10.0` | `OMSOrder` or `TimeoutError` | `BasketCoordinator._wait_terminals` | `_fill_futures` |
| `_map_ib_status` | Map IB string to `OMSOrderStatus` | `ib_status:str` | `OMSOrderStatus` | `on_order_status`, `on_open_order` | — |
| `_apply_mapped_status` | Apply without regressing terminal | `order, mapped_status, qty_filled, qty_remaining, now` | mutates `order.status/filled_quantity/remaining_quantity` | callbacks | — |
| `_notify_future_if_terminal` | Resolve `Future` if terminal | `order` | — | callbacks | `loop.call_soon_threadsafe` |
| `on_order_status` | Handle `orderStatus` | `orderId,status,filled,remaining,avgFillPrice,...` | updates `order`, future, emits `BROKER_ACK/FILL/PARTIAL_FILL` | `TWSClient.orderStatus` (via listener) | `_apply_mapped_status`, `_callback_event_kinds` |
| `_callback_event_kinds` | Decide which events to emit (`BROKER_ACK` once, `FILL` once, `PARTIAL_FILL` deduped) | `order, source, exec_id, is_new` | `list[str]` | callback dispatch | — |
| `on_open_order` | Handle `openOrder(orderState.status)` | `orderId, contract, order, orderState` | applies mapped status | `TWSClient.openOrder` | — |
| `on_exec_details` | Handle fill (`cumQty`/`execId`) | `reqId, contract, execution` | accumulates `BrokerExecution`, updates `filled_quantity`, emits `FILL/PARTIAL_FILL` | `TWSClient.execDetails` | — |
| `on_exec_details_end` | Log | `reqId` | — | — | — |
| `on_commission_report` | Attach commission to execution | `commissionReport {execId,commission,currency,realizedPNL}` | mutates `record.commission`, emits `COMMISSION` | `TWSClient.commissionReport` | — |
| `on_error` | Map TWS error codes to order status | `reqId, errorCode, errorString` | terminal `CANCELLED/REJECTED/ERROR` vs ignore warnings (`399,2109`, `2000-2999`, `10000-11000`) | `TWSClient.error` | — |
| `on_connection_closed` | Mark all non-terminal orders `ERROR` | — | mutates orders, resolves futures | `TWSClient.connectionClosed` | — |

---

## 3. OrderSubmitPacer — `backend/app/oms/submit_pacer.py:12`

**Purpose:** The single global inter-order throttle: at most 1 `placeOrder` per 0.2s across **all** accounts on one TWS socket. Prevents burst IBKR pacing violations.

**State:** `min_interval_sec:float`, `_lock:asyncio.Lock`, `_last:float` (monotonic of last submit).

**Methods:**
- `__init__(min_interval_sec=0.2)`: validates `>=0`.
- `async acquire() -> bool` (`submit_pacer.py:22-36`): under lock, compute `wait = min_interval - (now - _last)`, `await asyncio.sleep(wait)` if `>0`, update `_last = monotonic()`, return `delayed` bool (logged + stored as `order.pacer_delayed`). Called by `IBKRExecutionAdapter.submit_order` before `placeOrder`. Used in retry logging (`BasketCoordinator._retry_incomplete`).

---

## 4. IBKRExecutionScheduler — `backend/app/broker/ibkr/scheduler.py:27` **(tests-only)**

> ⚠️ Not used in production. Production pacing is `OrderSubmitPacer(0.2s)` (`main.py:48`). This scheduler is only exercised by tests.

**Purpose:** Centralized token-bucket rate limiter with priority lanes + emergency reserve + concurrency semaphore. Models IBKR 50 msg/sec ceiling with 30/24/6 app/emergency split.

**State:** `_global_max_rate/_normal_max_rate/_emergency_reserve`, `_semaphore(max_concurrent=10)`, `_lock`, `_global_tokens/_normal_tokens/_last_fill`, `_priority_locks: dict[0..4 → Lock]`, `metrics{total_requests, requests_by_priority, requests_by_type, throttled_count, ...}`.

**Priority constants:** `PRIORITY_EMERGENCY_FLATTEN=0`, `ORDER_EXECUTION=1`, `CONTRACT_DETAILS=2`, `MARKET_DATA=3`, `DIAGNOSTIC=4`. Limits `DEFAULT_GLOBAL_APP_BUDGET=30`, `NORMAL=24`, `EMERGENCY=6`.

**Methods:**
- `__init__(max_rate_per_sec, normal_rate_limit, emergency_reserve, max_concurrent)`.
- `async _acquire_token(priority)`: replenish buckets by elapsed, block-sleep `1/wait_rate` until token available (emergency uses global only; others need both).
- `async execute_paced(func,*args, priority=1, request_type="general",**kwargs) -> Any`: clamps priority 0-4, `async with _priority_locks[priority]`, `_acquire_token`, `async with _semaphore`, update metrics, `await func` if coroutine else `to_thread(func)`.

---

## 5. OMSService — `backend/app/oms/oms_service.py:17`

**Purpose:** Minimal OMS: validate `RMS PASS`, enforce in-memory duplicate guard `_submitted_signals`, iterate `intent.legs`, build `OMSOrder` per leg, delegate to `IBKRExecutionAdapter`.

**State:** `_adapter:IBKRExecutionAdapter`, `_orders:dict[str,OMSOrder]`, `_submitted_signals:set[str]` (`"{account_id}:{signal_id}"`).

**Methods:**
- `get_order / get_all_orders`: dict reads, used by `/orders` routes.
- `async submit_intent(intent,rms_result, override_internal_id, limit_price, order_type="LIMIT") -> ExecutionResult` (`oms_service.py:41-160`): rejects on `rms_result.outcome != PASS` (REJECTED order), rejects on duplicate key, rejects on empty legs, else loops legs via `_submit_leg`, captures `first_error`, returns `ExecutionResult` with `order=submitted[0]`, `orders=submitted`, `success` = no REJECTED/ERROR.
- `async submit_one_leg(intent,rms_result,index, oms_received_at, override_internal_id, limit_price, order_type) -> OMSOrder` (`162-199`): single-leg entry for `BasketCoordinator`; re-checks duplicate + RMS, delegates to `_submit_leg`.
- `async _submit_leg(intent,rms_result,index, oms_received_at, override_internal_id, limit_price, order_type) -> OMSOrder`: resolves instrument (`resolve_leg` if `leg.resolved` absent), `InstrumentResolutionError → REJECTED`, else creates `OMSOrder(pending, leg_index, resolved)`, stamps `timestamps`, calls `adapter.submit_order`, on exception marks `ERROR`.
- `_leg_order_id(signal_id,index,leg_count,override_internal_id,account_id) -> str` (`295-311`): `ORD-{account_id}-{signal_id}[-L{index}]` or overridden.
- `_leg_limit_price(leg,limit_price,leg_count)` (`313-321`): per-leg if multi-leg; else override.
- `_create_rejected_order(intent,reason,oms_received_at,override_internal_id) -> OMSOrder`: REJECTED audit row using first leg as snapshot; stored in `_orders`.
- `async cancel_order(internal_order_id) -> OMSOrder` (`357-363`): lookup + `adapter.cancel_order`.

---

## 6. Basket & BasketCoordinator — `backend/app/oms/basket.py:11` + `backend/app/oms/coordinator.py:48`

### Basket (`basket.py:11`)

- **`BasketState` enum:** `PENDING, EXECUTING, OPEN, CLOSED, UNWINDING, COMPENSATED, CRITICAL`.
- **`Basket` dataclass:** `account_id:int|None`, `trade_id:str`, `strategy_id`, `action:str`, `intended_leg_count:int`, `state:BasketState=PENDING`, `id:int|None` (DB pk), `signal_pk:int|None`, `orders:list[OMSOrder]`, `compensation_orders:list[OMSOrder]`, `updated_at`.
- **`BasketExecutionResult`:** `basket`, `intent`, `orders`, `compensation_orders`; `state` proxy, `success` iff `OPEN|CLOSED`.

### BasketCoordinator (`coordinator.py:48`)

**Purpose:** The generic multi-leg atomicity engine: submit N child orders (serially, but with global pacer), wait for fills, retry incomplete (paper only, within window), cancel working, compensate filled, or escalate to `CRITICAL` (blocks future OPENs for the `(account_id,strategy_id)` pair).

**State:** `_oms:OMSService`, `_session_factory`, `_fill_timeout/_cancel_timeout`, `_retry_policy:ExecutionRetryPolicy|None`, `_rms_engine/_rms_context`, `_paper_retries_allowed:bool`, `_retry_ids:set[str]` (dedupe RETRY keys), `_critical:set[tuple[int,str]]` (the gate), `_order_baskets:dict[str,Basket]` (order→basket), `_loop`.

**Key methods:**

| Method | Purpose | Inputs | Returns/Side |
|---|---|---|---|
| `is_open_blocked / mark_critical` | Gate for new OPENs (`OrderManager._evaluate_and_submit_locked`) | `account_id, strategy_id` | bool / add to `_critical` |
| `apply_retry_policy` | Hot-reload from `execution_settings` | `policy:ExecutionRetryPolicy, paper_retries_allowed:bool` | validate, set `fill_timeout` |
| `hydrate_critical_from_db` | Load CRITICAL baskets on restart | — | populates `_critical` |
| `async execute(intent,rms_result, order_type, signal_pk) -> BasketExecutionResult` | Main orchestration | `OrderIntent (already resolved), RMS PASS, signal_pk` | Inserts basket `PENDING→EXECUTING`, sequential `oms.submit_one_leg` per leg (with `BASKET_CREATED/BASKET_EXECUTING/ORDER_CREATED/ORDER_SUBMITTED` events), waits `fill_timeout`, retries, else compensates or open/close |
| `async recover_incomplete_baskets` | On hydrate, escalate `EXECUTING/UNWINDING` rows to `CRITICAL` if snapshot unavailable | — | `fetch_broker_order_snapshot` then `mark_critical` |
| `_basket_complete(intent,orders)->bool` | Every leg `filled >= intended`? (`_FILL_EPS 1e-8`) | — | bool | called after wait + retries |
| `async _wait_terminals(orders,timeout)` | Gather `adapter.wait_for_terminal_or_fill` per non-terminal | `list[OMSOrder]` | — | — |
| `_retries_enabled()->bool` | Check `policy.enabled && paper_retries_allowed && max_retries>0 && rms wired` | — | bool | — |
| `async _cancel_working(submitted,...)` | Cancel non-terminal orders | — | bool success | — |
| `async _retry_incomplete(intent,submitted,order_type,signal_pk,basket) -> list[OMSOrder]` | Paper-only retry of shortfall within `retry_window_sec`, per-leg `ATTEMPT` loop, RMS re-eval per retry leg, pacer logging | — | new orders | calls `_retry_intent` + `oms.submit_one_leg` |
| `_retry_intent(original,orig_leg,remaining,index,attempt) -> OrderIntent` | Synthetic single-leg RETRY intent `"{signal_id}:RETRY:L{index}:{attempt}"` | — | `OrderIntent` | — |
| `async _compensate_filled(original,submitted,order_type,signal_pk,basket) -> list[OMSOrder]` | Reverse each cum-filled leg via `CLOSE` intent (`:UNWIND:`) | — | compensation orders | `oms.submit_intent` per leg, mark `is_compensation` |
| `async _fail_critical(basket,intent,submitted,compensation,signal_pk)` | Mark `CRITICAL`, `mark_critical`, persist, emit `BASKET_CRITICAL` | — | — | — |
| `async _persist_basket / _persist_child / _ensure_signal_pk` | DB mirrors: `BasketRepository.upsert`, `OrderRepository.record_oms_order`, `ExecutionRepository.upsert` | — | — | — |
| `_on_broker_order_state(order,kind)` | Listener from adapter → `run_coroutine_threadsafe(_persist_broker_snapshot)` | `(order, kind in BROKER_ACK/PARTIAL_FILL/FILL/COMMISSION/...)` | schedules persist | registered at `__init__` via `adapter.add_order_state_listener` |
| `async _persist_broker_snapshot / _event` | Persist order snapshot + deduped `event_log` via `EventRepository` | `order,kind,idempotency_key` | — | — |

**Event kinds emitted:** `BASKET_CREATED`, `BASKET_EXECUTING`, `ORDER_CREATED/CLOSE_ORDER_CREATED`, `ORDER_SUBMITTED/CLOSE_ORDER_SUBMITTED`, `BASKET_OPEN/CLOSED`, `BASKET_UNWINDING`, `COMPENSATION`, `BASKET_COMPENSATED`, `BASKET_CRITICAL`, `BASKET_RECOVER_CRITICAL`, `AUTO_SQUARE_OFF_RETRY/BLOCKED`, `BROKER_ACK`, `FILL`, `PARTIAL_FILL`, `COMMISSION`, `KILL_SWITCH_*`.

---

## 7. RMSEngine & Checks — `backend/app/rms/engine.py:37` + `backend/app/rms/checks/*.py`

### RMSEngine (`engine.py:37`)

**Purpose:** Sequential, short-circuiting pipeline of 5 fixed checks. `ADJUST` carries forward mutated intent; `REJECT/HALT` stops pipeline and fails the whole intent.

**State:** `checks: list[BaseRMSCheck]` = `[DuplicateCheck, StrategyCheck, ContractMonthCheck, OpenPositionLimitCheck, MoneyPerStockCheck]` by default (`get_default_checks`).

**Method:** `evaluate(order_intent,rms_context) -> RMSResult` (`engine.py:48-123`): logs start, iterates checks, collects `CheckResult`, on `REJECT/HALT` returns immediately with `check_number/reason`; on `ADJUST` swaps `current_intent`; after loop returns `PASS`.

### BaseRMSCheck (`rms/checks/base.py:8`) `ABC`
- `check_number:int`, `check_name:str`, `evaluate(intent,context)->CheckResult` abstract.

### DuplicateCheck (`rms/checks/duplicate.py:7`) — **Check 2**
- Rejects if `(account_id, strategy_id, signal_id)` already in `context.processed_signals`. OPEN only; CLOSE bypasses (needed after restart). `REJECT` with `DUPLICATE_SIGNAL`.

### StrategyCheck (`rms/checks/strategy.py:14`) — **Check 3**
- Rejects if `strategy_id` has no `StrategyConfig` or `strategy.enabled` false / unknown. `REJECT "UNKNOWN_STRATEGY"`.

### ContractMonthCheck (`rms/checks/contract_month.py:58`) — **Check 4**
- For futures-like legs with `contract_month`, consults `context.rollover_checker` or `target_rollover_month`. If in rollover window (`rollover_window_days`) and target set, `ADJUST` intent with legs' `contract_month = target_rollover_month`. Stock/CFD legs pass through. Can also `REJECT` if month malformed.

### OpenPositionLimitCheck (`rms/checks/position_limit.py:14`) — **Check 7**
- Enforces `account_open_limits[(account_id,strategy_id)]` (from `AllocationModel.max_open_positions`) and `strategy_configs[strategy_id].max_open_positions`. Only counts `OPEN` action. `REJECT "OPEN_POSITION_LIMIT_EXCEEDED"` if `open_positions[(account_id,strategy_id)] >= limit`.

### MoneyPerStockCheck (`rms/checks/money_per_stock.py:17`) — **Check 8**
- For each leg, checks `effective_notional` against per-leg budget: `per_symbol_limits[(account_id,symbol)]` if set else `default_symbol_limits[account_id]` else `strategy_config.money_limit_per_symbol`. Sums `symbol_exposures[(account_id,symbol)]`. On OPEN, `REJECT` if `exposure + notional > limit`; on CLOSE, allows (reduces exposure). Uses `exposure_key`/`per_symbol_limits`.

---

## 8. OrderManager — `backend/app/services/order_manager.py:85`

**Purpose:** The top-level façade: webhook payload → `Signal` → account fanout → sizing → RMS → execution claims → basket/OMS → runtime state + live PnL.

**Dependencies:** `OMSService`, `RMSEngine/RMSContext`, `ModelBlueStrategy`/sizer/trade_book/persistence, `StrategyRegistry`, `DatabaseStrategyAccountRouter`, `DatabaseInstrumentCatalog`, `ModelBlueExecutionPersistence`, `KillSwitch` cache, `LivePnlService`.

**State:** `_oms`, `_symbol/_quantity/_order_type="MARKET"` (legacy fallback), `_strategy_id`, `_session_factory`, `_persistence`, `_committed_capital_provider`, `_account_router`, `_rms_engine/_rms_context`, `_model_blue_strategy/_model_blue_sizer/_model_blue_trades`, `registry`, `_live_pnl:LivePnlService|None`, `_baskets:BasketCoordinator|None` (only if `type(oms) is OMSService`), `_exposure_locks:dict[object,Lock]`, `_instrument_catalog`.

**Methods (selected, file order):**

| Method | Purpose | Inputs→Returns / Side effects | Caller → Calls |
|---|---|---|---|
| `__init__` | Wire deps, init RMS context, baskets | — | `main.lifespan` → `BasketCoordinator`, `DatabaseInstrumentCatalog` |
| `async hydrate_runtime_from_db` | Rebuild `processed_signals`, `open_positions`, `symbol_exposures`, per-symbol limits, critical baskets, kill-switch cache, execution policy | DB reads `SignalRepository.list_processed_open_keys`, `PositionRepository.list_open`, `PerSymbolLimitModel`, `AccountModel`; then `baskets.hydrate_critical_from_db`, `recover_incomplete_baskets`, `hydrate_kill_switch_cache`, `reload_execution_policy` | `main.lifespan` | basket + kill_switch hydrations |
| `_apply_symbol_limits(limits,accounts)` | Replace `rms_context.per_symbol_limits/default_symbol_limits` | lists of rows | — |
| `async reload_rms_limits` | Refresh limits from DB (after config PUT) | — | `PATCH/PUT /config/*` → `_apply_symbol_limits` |
| `async reload_execution_policy` | Load `execution_settings` row→ `ExecutionRetryPolicy` → `baskets.apply_retry_policy` | — | `main.lifespan`, `PATCH /config/execution` |
| `async hydrate_live_pnl` | Re-subscribe_ticks for OPEN positions after TWS up; ensure CFD instruments for symbols with `secType=CFD` | — | `main.lifespan` after connect | `ensure_cfd_instruments_for_symbols`, `_instrument_snapshot_for_legs`, `LivePnlService.hydrate_from_position_rows` |
| `_add_row_exposure(row)` | Book `abs(qty)*entry_mark` per leg into `rms_context.symbol_exposures` | `PositionModel` | mutates context | `hydrate_runtime_from_db` |
| `parse_inbound_payload(payload,timestamp,request_id,capture_data)->Signal` | Delegate to `parse_tradingview_payload` | payload + metadata | `Signal` | `worker_pool._execute_job` → `parse_tradingview_payload` |
| `async process_signal(signal)->OMSOrder|None` | Legacy shim: `process_signal_execution` → first order | `Signal` | `OMSOrder` | callers | `process_signal_execution` |
| `async process_signal_execution(signal)->FanoutExecutionResult|ExecutionResult|None` | Main entry: HOLD→None, else `persist NEW`, `try _process_signal_execution_inner`, on all_rejected mark `REJECTED`, on exception mark `REJECTED` | `Signal` | result | `WorkerPool._execute_job` → `_process_signal_execution_inner`, `_persist_inbound_signal` |
| `async _process_signal_execution_inner(signal,inbound_row)` | Lookup handler, fanout or single-name path | — | `FanoutExecutionResult` | → `registry.get`, `_fanout_accounts` or `_process_legacy_single_name` |
| `async _fanout_single_account(signal,handler,ctx,inbound_pk,raise_if_single)` | Per-account: ensure config, kill-switch block on OPEN, `handler.build_intent`, ` _evaluate_and_submit` | `AccountExecutionContext` | `AccountExecutionOutcome` | ` _fanout_accounts` gather | `handler.build_intent`, `_evaluate_and_submit`, `is_account_kill_switch_active` |
| `async _fanout_accounts` | `asyncio.gather` over contexts | `list[AccountExecutionContext]` | `FanoutExecutionResult` | — | `_fanout_single_account` |
| `async _process_legacy_single_name` | Legacy single-symbol `BUY/SELL → OrderIntent` (no Model Blue) | — | `ExecutionResult` | — | `_evaluate_and_submit` |
| `async _acquire_execution_claim(intent) -> str|None` | Durable barrier INSERT before broker (own txn) | `OrderIntent` | `dedupe_key` | `_evaluate_and_submit_locked` → `ExecutionClaimRepository.acquire` |
| `async _seal_execution_claim(dedupe_key)` | `mark_executed` after success | — | bool | — |
| `async _resolve_failed_claim(dedupe_key,intent)` | `count_orders_emitted` → hold if any orders else `release` | — | — | — |
| `async _get_exposure_lock / @asynccontextmanager _exposure_guard` | Serialize `symbol_exposures` read-modify-write across strategies sharing a symbol (sorted keys to avoid deadlock) | `intent` | lock context | `_evaluate_and_submit` |
| `async _evaluate_and_submit(intent,signal,handler,inbound_pk) -> ExecutionResult` | Guarded RMS+instrument+basket path | — | `ExecutionResult` | `_exposure_guard` → `_evaluate_and_submit_locked` |
| `async _evaluate_and_submit_locked` | Zero-qty guard, `rms_engine.evaluate`, `_audit_rms`, block on `is_open_blocked` (BASKET_CRITICAL), `resolve_instruments`, acquire claim, branch `baskets.execute` vs `oms.submit_intent`, seal | — | `ExecutionResult` | → `rms_engine.evaluate`, `_resolve_instruments`, `_acquire_execution_claim`, `baskets.execute`, `_seal_execution_claim`, `_update_runtime_state`, `live_pnl.watch_open/unwatch`, `_record_unsettled_exposure` |
| `async _audit_rms(intent,rms_result,signal_pk)` | Append `event_log process=rms kind=RMS_<outcome>` | — | — | — | `EventRepository.append` |
| `_intent_with_fills(intent,orders)->OrderIntent` | Replace legs with weighted avg fill price + `filled_quantity` | `OrderIntent, list[OMSOrder]` | `OrderIntent` | `_update_runtime_state`, `_record_unsettled_exposure` |
| `_record_unsettled_exposure(intent,orders)` | Book exposure for CRITICAL/UNWINDING partial fills (so other strategies' `MoneyPerStockCheck` sees real risk) | — | mutates `symbol_exposures` | `_evaluate_and_submit_locked` failure branch |
| `async _update_runtime_state(intent,exec_res,handler,sized_from)` | If handler exists: OPEN→ `processed_signals.add`, `open_positions++`, `symbol_exposures+=notional`; CLOSE→ decrement; call `handler.after_submit` and persist signal `PROCESSED`; else legacy symbol map | — | — | — |
| `async record_rejected_inbound(payload,capture_data,reason)` | Persist parse failure via `SignalRepository.record_rejected_payload` | — | — | `Recovery/worker` fallback |
| `async _persist_inbound_signal(signal,status,reject_reason)` | Upsert `signals` row + `event_log` `SIGNAL_RECEIVED/CLOSE_SIGNAL_RECEIVED + SIGNAL_PERSISTED` | — | row | — | `SignalRepository.record_inbound`, `EventRepository` |
| `_ensure_strategy_config` | Ensure `rms_context.strategy_configs` entry exists | `strategy_id, max_open_positions` | — | — |
| `async _resolve_instruments(intent,signal_pk) -> OrderIntent` | CFD discovery (if needed), snapshot catalog, `attach_resolved` | `OrderIntent` | resolved intent | `ensure_cfd_instruments_for_symbols`, `attach_resolved`, `_audit_instruments_resolved` |

---

## 9. ExecutionWorkerPool — `backend/app/services/worker_pool.py:50`

**Purpose:** 10-worker durable queue: `FOR UPDATE SKIP LOCKED` claim, domain-locked execution, lease heartbeat, stale-job reclaimer, execution-claim reclaimer.

**State:** `session_factory`, `order_manager`, `worker_count=10`, `lease_duration_sec=30`, `reclaim_interval_sec=15`, `claim_stale_after_sec=300`, `idle_poll_interval_sec=0.5`, `_workers:list[Task]`, `_reclaimer_task`, `_running`, `_domain_locks:dict[tuple[str,str],Lock]` keyed by `(account_scope,strategy_id)`.

**Methods:**

| Method | Purpose | Caller → Calls |
|---|---|---|
| `async start / stop` | Spawn `worker_count` tasks + reclaimer; cancel all | `main.lifespan` |
| `async _reclaimer_loop` | Loop `sleep(reclaim_interval)` → `SignalJobRepository.reclaim_stale_jobs()` (requeued/quarantined/dead_lettered) + `ExecutionClaimRepository.reconcile_stale_claims(stale_after_sec)` | `_reclaimer_loop` → repos |
| `async _worker_loop(worker_id)` | Loop `claim_next_jobs` → ` _process_claimed_job` else idle sleep | workers |
| `async _claim_job(worker_id) -> list[SignalJobModel]` | `repo.claim_next_jobs(worker_id, limit=1, lease_duration)` | `_worker_loop` |
| `async _process_claimed_job(worker_id,job)` | Bind log context, start ` _lease_heartbeat`, acquire `domain_lock`, check `lease_lost`, ` _execute_job`, cancel heartbeat | `_worker_loop` |
| `async _lease_heartbeat(job_id,worker_id,cancel_event,lease_lost)` | Every `lease_duration/3` call `repo.heartbeat_lease`; on 0 rows set `lease_lost` and log | background task |
| `async _write_status(job_id,status,worker_id,lease_lost,error)->bool` | `repo.update_status(...,fence=True, lease_duration)`; on 0 → `lease_lost.set` | `_execute_job`, `_process` |
| `async _execute_job(worker_id,job,lease_lost)` | `write PROCESSING` (fenced), `order_manager.parse_inbound_payload`, `process_signal_execution`, if `lease_lost` abort without terminal write; map result to `COMPLETED/REJECTED/FAILED` via ` _write_status` | `_process_claimed_job` |
| `compute_idempotency_key(payload)->(strategy_id,signal_id,trade_id|None,hex)` | Pure: normalize ids,  `signal_id = trade_id[:CLOSE]` for CLOSE, `sha256("{strategy}:{signal}:{action}")` | `webhooks.receive_tradingview_webhook` |

---

## 10. RecoveryManager — `backend/app/services/recovery.py:24`

**Purpose:** Startup crash recovery: scan `signal_jobs` (`CLAIMED|PROCESSING|RECOVERY_REQUIRED`) + `baskets` (`EXECUTING|UNWINDING`), best-effort `fetch_broker_order_snapshot`, reconcile execution claims (`stale_after 0`), then per job: if `count_orders_emitted>0` → `RECOVERY_REQUIRED` (quarantine), if `attempts>=max` → `DEAD_LETTER`, else → `QUEUED` (requeue). Then `order_manager.hydrate_runtime_from_db` to rebuild critical/balances.

**State:** `session_factory`, `order_manager`.

**Methods:** `async run_startup_recovery()->int` (`recovery.py:35-152`), called in `main.lifespan` between TWS connect and worker start.

---

## 11. KillSwitchService & helpers — `backend/app/services/kill_switch.py:142`

**Purpose:** Durable, idempotent, bounded-parallel emergency flatten; authoritative `kill_switch_operations` table + in-memory gate `_KILL_SWITCH_ACTIVE_ACCOUNTS`.

**Module state:** `_KILL_SWITCH_ACTIVE_ACCOUNTS:set[int]`, `_ARMED_STATUSES = (ACTIVATING,FLATTENING,RECONCILING,RETRYING,FLAT,COMPLETE,UNRESOLVED)`.

**Helpers (module-level):**
- `is_account_kill_switch_active(account_id)->bool` (`kill_switch.py:60`) — read cache.
- `_arm_kill_switch_cache / clear_account_kill_switch_cache` — mutate cache (internal).
- `async hydrate_kill_switch_cache(session_factory)->set[int]` (`75-102`): `SELECT DISTINCT account_id WHERE status IN _ARMED_STATUSES`; replaces cache; called in `OrderManager.hydrate_runtime_from_db` before any signal.
- `async clear_account_kill_switch(session_factory,account_id,cleared_by)->int` (`105-139`): `UPDATE ... SET status=CLEARED WHERE status IN _ARMED_STATUSES` → `rowcount`, discard cache, log. The `ordered` clear path for Settings UI.

**KillSwitchService (`kill_switch.py:142`):**

| Method | Purpose | Returns | Side effects |
|---|---|---|---|
| `__init__(session_factory,order_manager,max_concurrent_positions=5)` | Init semaphore=5 | — | — |
| `async initiate_square_off(account_id,requested_by)->(KillSwitchOperationModel,created_new)` (`156-227`) | Idempotent insert or reuse active op (`ACTIVATING|FLATTENING|RECONCILING|RETRYING`), count OPEN positions, arm cache, log | `operation, bool` | Inserts `ACTIVATING` row if none; else returns existing; arms `_KILL_SWITCH_ACTIVE_ACCOUNTS` |
| `async arm_account_kill_switch_only(account_id,requested_by)->(op,bool)` (`229-295`) | Same but checks `_ARMED_STATUSES` + cache; **no broker calls** | — | Used by `/emergency-kill-switch` |
| `async execute_flatten_operation_background(op_id)` (`297-302`) | `asyncio.create_task(_execute_flatten_operation(op_id))` (fire-and-forget off HTTP thread) | — | — | `initiate_square_off` → this |
| `async _execute_flatten_operation(op_id)` (`304-347`) | Transition `ACTIVATING→FLATTENING`, load OPEN positions, emit `KILL_SWITCH_ACTIVATED`, `gather(_flatten_single_position for pos)` bounded by semaphore, then `_reconcile_and_finalize` | — | — |
| `async _flatten_single_position(account_id,ibkr_account,pos,baskets_coord)->bool` (`349-467`) | Build `CLOSE` intent with reversed legs (`MARKET`, `intent_mode=EMERGENCY_FLATTEN`, `contract_month=202612`), resolve, `baskets.execute` with `MARKET`, on fully filled `PositionRepository.close_trade` + `event POSITION_CLOSE idempotent` | `success:bool` | places orders via basket |
| `async _reconcile_and_finalize(op_id,account_id,results)` (`469-534`) | Re-reconcile stale OPEN positions whose KILLSWITCH+CLOSE orders filled, compute `unresolved = len(remaining OPEN)` → `COMPLETE|UNRESOLVED`, `update_operation_completion`, emit `KILL_SWITCH_COMPLETED/UNRESOLVED` | — | updates `KillSwitchOperationModel` counts |
| `async _update_operation_completion(op_id,final_status,unresolved)` (`536-545`) | Set `status`, `unresolved_count`, `flattened_count = initial - unresolved` | — | — |

---

## 12. ModelBlueStrategy — `backend/app/services/model_blue/strategy.py:40`

**Purpose:** First registered `StrategyHandler`. Parses TradingView Model Blue OPEN/CLOSE, sizes OPEN via `ModelBlueSizer`, looks up CLOSE from trade book, persists via `ModelBlueExecutionPersistence`.

**State:** `_committed_capital_provider`, `_sizer:ModelBlueSizer|None`, `_trades:ModelBlueTradeBook` (default `InMemory`), `_persistence`.

**Methods:**

| Method | Purpose | Caller |
|---|---|---|
| `can_handle(strategy_id)->bool` | `is_model_blue_strategy` (case-insensitive `== "model_blue"`) | `StrategyRegistry.get` |
| `parse_payload(payload,timestamp,reason,raw_payload)->Signal` | → `parse_model_blue_payload` | `OrderManager.parse_inbound_payload` |
| `async build_intent(signal,account)->OrderIntent` | Dispatch `CLOSE→ _build_close_intent`, `OPEN→ _build_open_intent`, else `ModelBlueValidationError` | `OrderManager._fanout_single_account`, `_process_legacy...` |
| `async after_submit(signal,intent,exec_res)` | OPEN: build `OpenModelBlueTrade` from filled intent legs → `persistence.persist_open` else `trades.record_open`; CLOSE: `persist_close`/`close` | `OrderManager._update_runtime_state` |
| `async _resolve_committed` | `provider.get_committed` (awaitable or sync) | `_build_open_intent` |
| `async _build_open_intent(signal,account)->OrderIntent` | Duplicate OPEN check via `trades.get`, resolve sizer (`TemporarySettingsCommittedCapitalProvider(committed)` from `account.committed_notional` else provider), `sizer.size_open(signal) → 2 SizedLegs → OrderLegs`, `OrderIntent(signal_id=trade_id, strategy_id=model_blue, action=OPEN, legs=2, account_id, ibkr_account, market)` | `build_intent` |
| `async _build_close_intent(signal,account)->OrderIntent` | Lookup `open_trade = trades.get(trade_id, account_id)`, error `NO_OPEN_POSITION` if missing, reverse each `OpenModelBlueTradeLeg` (`BUY↔SELL`, qty=float, notional) → `OrderIntent(signal_id=f"{trade_id}:CLOSE", action=CLOSE, legs=2)` | `build_intent` |
| `_order_leg_from_sized(index,sized)->OrderLeg` | Wrap `SizedModelBlueLeg` → `OrderLeg(quantity=float, contract_month=2026-09, notional)` | `_build_open_intent` |

---

## 13. ModelBlueSizer — `backend/app/services/model_blue/sizer.py:49`

**Purpose:** Production OPEN pair sizer: IBKR rejects fractional STK (`error 10243`), so STK quantities are floored to whole shares; notional never exceeds allocated target.

**State:** `_committed_capital_provider:CommittedCapitalProvider`.

**Constants:** `MIN_ORDER_NOTIONAL=100`, `_QTY_QUANTUM=0.0001`, `_STK_QTY_QUANTUM=1`.

**Methods:**

| Method | Purpose | Caller |
|---|---|---|
| `size_open(signal)->(SizedModelBlueLeg,SizedModelBlueLeg)` (`sizer.py:55-125`) | Validates `model_blue`, `action==OPEN`, `direction∈±1`, `2 legs`, `committed>0`, `base_weight!=0`, then `_size_leg` for base (`target=committed`) and hedge (`target=committed*|hedge.weight|/|base.weight|`), logs result | `ModelBlueStrategy._build_open_intent` |
| `_size_leg(symbol,instrument_type,weight,price,direction,target_notional)->SizedModelBlueLeg` (`127-173`) | Price>0, `quantity = (target/price).quantize(STK 1 ROUND_DOWN else 0.0001 ROUND_HALF_UP)`, `<1 share → MIN_SHARE error`, `notional=qty*price <100 → MIN_NOTIONAL error`, `side=BUY if weight*direction>0 else SELL` | `size_open` |

---

## 14. PositionReconciler — `backend/app/services/position_reconciler.py:247`

**Purpose:** Background 30s loop: `TWSClient.request_positions_async` snapshot → `BrokerPositionRepository.replace_snapshot` (persist) → diff ledger OPEN nets vs broker snapshot (`classify_reconcile_diffs` producing `MATCH/LEDGER_GHOST/BROKER_ORPHAN/QTY_DRIFT/UNMAPPED_ACCOUNT` with `in_flight` flag for accounts with `EXECUTING/UNWINDING` baskets or `PROCESSING` jobs) → insert `PositionReconcileRunModel` + `event_log POSITION_RECONCILE`. Never auto-trades (flatten is via `BrokerFlattenService`).

**State:** `session_factory`, `client`, `interval_sec=30`, `request_timeout_sec=15`, `_task`, `_running`, `_sweep_lock`.

**Methods:** `async start/stop`, `async _loop` (run_once + sleep), `async run_once` (skip if lock held or disconnected, call `request_positions_async`, then `_persist_and_diff`), `async _persist_and_diff` (persist snapshot, compute `ledger_lines`, `diffs`, insert run row + event). Helpers: `build_ledger_net_lines`, `classify_reconcile_diffs`, `fetch_in_flight_accounts`, constants `RECONCILE_INTERVAL_SEC`, `QTY_EPSILON=1e-6`.

---

## 15. LivePnlService — `backend/app/services/pnl.py:75`

**Purpose:** Subscribe `TWSClient.reqMktData` for each OPEN pair leg, recompute `unrealized_leg = signed_qty*(mark-entry)` (+ hedge leg), coalesce persists to `positions.live_pnl` (throttled 1s + delayed commit).

**State:** `session_factory`, `_client`, `_next_req=50000`, `_by_req/_listeners_by_req/_marks/_quotes/_legs:dict[(account,trade, symbol)→quote]`, `_contract_reqs/_req_to_contract/_contract_health`, `_cooldowns`, `_loop`, `_catalog`, `_persist_lock`, `_pending_pnl/_last_persisted_pnl/_last_persist_at/_persist_in_flight/_persist_delayed`.

**Tick constants:** `_TICK_BID=1, ASK=2, LAST=4, CLOSE=9, MARK=37, DELAYED_*` sets, `_effective_mark = last or mid(bid,ask) or close`.

**Methods:**

| Method | Purpose | Caller |
|---|---|---|
| `__init__(session_factory,client)` | Register as `market_data_listener` | `main.lifespan` → `_live_pnl = LivePnlService` |
| `watch_open(intent)` | For `(account_id, trade_id)` not yet watched, sign each leg `BUY→+qty SELL→-qty` → ` _request_ticks` per leg | `OrderManager._evaluate_and_submit_locked` (OPEN) |
| `hydrate_from_position_rows(rows,catalog)` | Re-subscribe_ticks for persisted OPEN rows on restart (no order submit) | `OrderManager.hydrate_live_pnl` |
| `unwatch(account_id,trade_id)` | Pop `_legs`, cancel `cancelMktData` per reqId with no listeners left | `OrderManager` on CLOSED, reconciler |
| `on_error(reqId,errorCode,errorString)` | Handle `10089/10167→DELAYED_FALLBACK switch reqMarketDataType 3`, `354/300/321→NO_MARKET_DATA_ENTITLEMENT 10m cooldown`, `1100 lost / 1101 restored→_resubscribe_all_active`, `200 UNRESOLVED_CONTRACT_SPEC` | `TWSClient.error` |
| `on_tick_price(reqId,tickType,price)` | Update quote `last/bid/ask/close`, compute `_effective_mark`, collect updated trades → `_recompute` | `TWSClient.tickPrice` |
| `on_tick_size / on_market_data_type / on_reroute_mkt_data / on_connection_closed / get_market_data_health` | Aux | — |
| `_request_ticks(account_id,trade_id,leg)` | Skip `ZZZ*` test symbols, resolve leg (`resolve_leg` via `ibkr_market_data_contract_from_resolved`), qualify via `request_contract_details` if connected, deduplicate per contract key `("STK",symbol,exchange,currency,conId)`, respect cooldown, else `reqMktData(req_id,contract,"221")`, log | `watch_open`, `hydrate...` |
| `_recompute(account_id,trade_id)` | If every leg has `marks`, sum unrealized → coalesce `_pending_pnl` → `run_coroutine_threadsafe(_schedule_persist)` | `on_tick_price` |
| `_schedule_persist / _persist` | Throttle to 1s min + delayed callLater, persist `PositionRepository.update_live_pnl` | `_recompute` |

---

## 16. DatabaseStrategyAccountRouter — `backend/app/accounts/router.py:56`

**Purpose:** The sole account fanout: `strategy_id → list[AccountExecutionContext]` of **enabled** `Account×Allocation×Strategy` rows (`Account.enabled`, `Strategy.enabled`, `Allocation.enabled`, `total_margin>0`, `alloc_pct>0`). Never infers a default account.

**State:** `session_factory`.

**Method:** `async resolve(strategy_id)->list[AccountExecutionContext]` (`router.py:62-119`): `JOIN Account→Allocation→Strategy WHERE allocation.strategy_id==wanted` ordered by `Account.id`; `committed=total_margin*alloc_pct` (skip ≤0); builds `AccountExecutionContext(account_id, ibkr_account, strategy_id, total_margin, alloc_pct, committed_notional, target, stop, time_limit, max_open_positions)`; logs resolved list. Protocol `StrategyAccountRouter` with `StaticStrategyAccountRouter` (test harness, filters `contexts` by `strategy_id`).

---

## 17. AccountStrategyConfigService — `backend/app/accounts/config_service.py:25`

**Purpose:** Validation layer for Settings CRUD: alloc pct ∈[0,1], enabled sum ≤1, uniqueness, strategy existence, margin>0.

**State:** `session:AsyncSession`.

**Selected methods:** `get_account/get_allocation`, `validate_account_margin`, `validate_alloc_pct`, `validate_max_open_positions`, `enabled_alloc_pct_sum(account_id, exclude)`, `ensure_unique_subscription`, `ensure_strategy_exists`, `_validate_enabled_sum`, `create_account` (dup IBKR check, `upper`), `has_trading_history` (counts `Order/Execution/Position/Basket`), `check_account_deletable` (false if history), `delete_account` (cascades allocations/limits/kill ops), `update_account` (blocks IBKR change if history), `create_allocation/update_allocation`, `upsert_symbol_limit/delete_symbol_limit`, `get_or_create_execution_settings(update_execution_settings)` with `ExecutionRetryPolicy.validate`. Called exclusively by `routes/config.py`.

---

## 18. PositionBridge — `backend/demo_streaming/publisher.py:33`

**Purpose:** Demo read-only poller: diff structural vs pnl fingerprints, XADD to Redis Streams (`positions:stream`), never mutates trading state.

**State:** `session_factory`, `stream:PositionStream`, `poll_interval=2s`, `signal_watch_limit=500`, `pnl_emit_interval=5s`, `structural_fingerprints/pnl_fingerprints/status/last_payload/signal_fingerprints/_last_signal_id`, `_baseline_ready`, `_last_pnl_emit`.

**Methods:** `restore_baseline` (snapshot without emit), `poll_once()->list[dict]` (collect, emit `SIGNAL_RECEIVED` per new signal id, emit structural changes via `classify_event` → `OPEN/UPDATE/CLOSE` else throttled `POSITION_UPDATE`, emit `POSITION_CLOSED` for vanished OPENs reloaded from CLOSED rows), `run_forever` (restore + loop), helpers `_payloads_for_vanished`, `_collect`.

---

## 19. PositionStream — `backend/demo_streaming/stream.py:13`

**Purpose:** Redis Streams wrapper. `xadd(payload)` JSON-encodes each value, `MAXLEN ~10000 APPROX`; `xread(last_id,block_ms,count)` decodes; `listen` generator.

**State:** `redis:Redis`, `stream_name="positions:stream"`, `stream_maxlen`. Methods `ping`, `xadd`, `xread`, `listen`.

---

## 20. Other notable classes (summary)

| Class | File | Purpose |
|---|---|---|
| `StrategyRegistry` | `app/services/strategies/registry.py:8` | `dict[str, StrategyHandler]`; `get(strategy_id)` case-insensitive; used by `OrderManager` + `inbound.parse_tradingview_payload` |
| `StrategyHandler(ABC)` | `app/services/strategies/handler.py:13` | `can_handle, parse_payload, build_intent, after_submit, uses_per_leg_prices` |
| `ModelBlueTradeBook(Protocol) / InMemoryModelBlueTradeBook` | `app/services/model_blue/trade_book.py:15,31` | `get(trade_id,account_id), record_open, close` |
| `CommittedCapitalProvider(Protocol) / TemporarySettingsCommittedCapitalProvider` | `app/services/model_blue/allocation.py:16,27` | `get_committed(strategy_id)->Decimal`; DB impl at `db_allocation.DatabaseCommittedCapitalProvider` |
| `ModelBlueExecutionPersistence` | `app/services/model_blue/persistence.py:121` | `persist_open/persist_close` → `positions`, `signals`, `event_log` |
| `DatabaseModelBlueTradeBook` | `app/services/model_blue/db_trade_book.py:13` | DB-backed `ModelBlueTradeBook` (reads `positions` table) |
| `DatabaseCommittedCapitalProvider` | `app/services/model_blue/db_allocation.py:17` | `get_committed` = `total_margin*alloc_pct` via `Account×Allocation` |
| `SinglePairCloseService` | `app/services/position_close_service.py:36` | `close_pair(account_id,trade_id)` single-pair close (no kill switch) |
| `BrokerFlattenService` | `app/services/broker_flatten_service.py:35` | `flatten_line(ibkr_account,symbol,sec_type,con_id)` deduped MARKET reverse |
| `ExecutionClaimRepository` | `app/db/repositories/execution_claim_repository.py:35` | Durable dedupe barrier `acquire/mark_executed/release/reconcile_stale_claims/count_orders_emitted` + exceptions `DuplicateExecutionError/ExecutionInFlightError/ClaimNeedsReconciliationError` |
| `InstrumentRecord / ResolvedInstrument` | `app/instruments/models.py:12,27` | DB-mirrored instrument + `ibkr_contract_from_resolved` target |
| `BrokerPositionLine / PositionSnapshotCollector` | `app/broker/ibkr/positions.py:8,22` | `BrokerPositionLine(ibkr_account,con_id,symbol,sec_type,currency,exchange,quantity,avg_cost)` + `reset/wait/snapshot` |

---

## 13. Function Reference

> **Source file:** `docs/reference/functions.md`  —  original heading: *Functions Reference — Code-Accurate*

> Each entry: file, purpose, signature, side effects, callers, exceptions.

---

## 1. `compute_idempotency_key` — `backend/app/services/worker_pool.py:27`

```python
def compute_idempotency_key(payload: dict[str,Any]) -> tuple[str,str,str|None,str]:
```

**Purpose:** Deterministic webhook dedupe: derive `(strategy_id, signal_id, trade_id, idempotency_key=hex(sha256("{strategy_id}:{signal_id}:{action}")))`.

**Params:** `payload` must be the raw TradingView JSON dict (may contain `strategy/strategy_id`, `trade_id/signal_id`, `action`).

**Returns:** `strategy_id` (normalized lowercase via `normalize_strategy_id`), `signal_id` (`trade_id` or `trade_id:CLOSE` when `action==CLOSE` and suffix not already present, or synthetic `SIG-<12hex>` if missing), `trade_id|None` (the normalized trade_id before CLOSE-suffix, or None), `idempotency_key` hex digest.

**Side effects:** None pure. Note: changing any input rotates hash; migration `a4c7e2f10938` backfilled existing rows after adding strategy to key.

**Callers:** `app.api.routes.webhooks._process_tradingview_webhook` (`webhooks.py:232`).

**Exceptions:** None (all missing keys fall back to defaults).

---

## 2. `parse_model_blue_payload` — `backend/app/services/model_blue/parser.py:24`

```python
def parse_model_blue_payload(payload: dict[str,Any], *, timestamp: datetime, reason: str, raw_payload: dict|None) -> Signal:
```

**Purpose:** Strict validator turning a real TradingView Model Blue OPEN/CLOSE JSON into a domain `Signal`. Exactly 2 buckets with `underlying→symbol`, `legs[0].instrument_type`, `legs[0].weight`, `legs[0].price` (+ `payload_side`). `strategy` must be `model_blue` case-insensitive.

**Params:** `payload` raw webhook dict; `timestamp` wall time; `reason` e.g. `"TradingView webhook request_id=..."`; `raw_payload` capture envelope forwarded into `Signal.raw_payload` for audit.

**Returns:** `Signal(signal_type=BUY for OPEN else SELL, signal_id=trade_id, strategy_id="model_blue", action="OPEN"|"CLOSE", trade_id, direction±1, market|None, legs=tuple[SignalLeg,SignalLeg] for OPEN else empty)`.

**Side effects:** Logs `Model Blue parse OPEN/CLOSE` with trade_id/legs.

**Callers:** `ModelBlueStrategy.parse_payload` (`strategy.py:68`), which is called by `parse_tradingview_payload`.

**Exceptions:** `ModelBlueValidationError(ValueError)` for: strategy mismatch, missing `trade_id`, invalid `action`, invalid `direction` (not ±1), missing `buckets`, wrong leg count (`≠2`), invalid bucket/leg structure, missing `symbol`/`instrument_type`/`weight==0`/`price≤0`.

**Helpers:** `_parse_direction(raw)->int`, `_parse_open_legs(buckets)->tuple[SignalLeg,SignalLeg]`, `_parse_price(raw,index)->Decimal`.

---

## 3. `parse_tradingview_payload` — `backend/app/services/strategies/inbound.py:17`

```python
def parse_tradingview_payload(payload: dict, *, timestamp: datetime, request_id: str, capture_data: dict, registry: StrategyRegistry|None) -> Signal:
```

**Purpose:** Strategy dispatch: lookup `strategy`/`strategy_id` in `registry` (default `_PARSE_REGISTRY = StrategyRegistry([ModelBlueStrategy()])`); if a handler `can_handle` matches, delegate to `handler.parse_payload(... reason=f"TradingView webhook request_id={request_id}", raw_payload=capture_data)`; otherwise fallback to `parse_legacy_signal` (unknown strategies).

**Params:** same as above plus `capture_data` capture envelope.

**Returns:** `Signal`.

**Callers:** `OrderManager.parse_inbound_payload` (`order_manager.py:347`).

**Exceptions:** Propagates `ModelBlueValidationError` or legacy parse errors.

---

## 4. `hydrate_runtime_from_db` — `backend/app/services/order_manager.py:171`

```python
async def hydrate_runtime_from_db(self) -> None:
```

**Purpose:** The single restart replay: rebuild every in-memory RMS gate from Postgres before any signal is processed.

**Flow:**
1. `SignalRepository.list_processed_open_keys()` → fill `rms_context.processed_signals` with `(strategy_id,signal_id)` and also expand to `(account_id,strategy_id,signal_id)` per `account_router.resolve`.
2. `PositionRepository.list_open()` → increment `rms_context.open_positions[(account_id,strategy_id)]`, add to `processed_signals`, call `_add_row_exposure` per row.
3. `PerSymbolLimitModel` + `AccountModel` → `_apply_symbol_limits`.
4. `baskets.hydrate_critical_from_db` + `recover_incomplete_baskets`.
5. `hydrate_kill_switch_cache(session_factory)` (rebuild `_KILL_SWITCH_ACTIVE_ACCOUNTS` before any OPEN).
6. `reload_execution_policy`.

**Side effects:** Mutates `rms_context`, `_critical`, kill-switch cache, logs counts.

**Callers:** `app.main.lifespan` after TWS connect, and `RecoveryManager.run_startup_recovery` tail.

**Exceptions:** Caught and logged by caller; missing `session_factory` is a no-op.

---

## 5. `process_signal_execution` — `backend/app/services/order_manager.py:366`

```python
async def process_signal_execution(self, signal: Signal) -> FanoutExecutionResult|ExecutionResult|None:
```

**Purpose:** The top-level signal orchestrator: `HOLD→None`, else persist `NEW`, `await _process_signal_execution_inner(signal)`, if result `all_rejected` persist `REJECTED` with aggregated reasons, else on exception persist `REJECTED` and re-raise.

**Returns:** `None` for HOLD; otherwise `FanoutExecutionResult` (multi-account) or `ExecutionResult` wrapped via `from_single`.

**Callers:** `ExecutionWorkerPool._execute_job` (workers), tests, `process_signal` shim.

**Exceptions:** Re-raises after persist; HOLD does not raise.

---

## 6. `evaluate` (RMS) — `backend/app/rms/engine.py:48`

```python
def evaluate(self, order_intent: OrderIntent, context: RMSContext) -> RMSResult:
```

**Purpose:** Sequential short-circuit pipeline described in `classes.md` #7. See engine section for `ADJUST`/`REJECT`/`HALT` rules.

**Callers:** `OrderManager._evaluate_and_submit_locked` (single intent), `BasketCoordinator._retry_incomplete` (each RETRY leg).

**Returns:** `RMSResult(outcome, intent, original_intent, check_number, reason, check_results, timestamp)`.

---

## 7. `submit_order` (adapter) — `backend/app/oms/ibkr_adapter.py:179`

```python
async def submit_order(self, order: OMSOrder) -> OMSOrder:
```

**Purpose:** The sole `placeOrder` path: check `is_connected()`, `await submit_pacer.acquire()` (sets `order.pacer_delayed`), dedupe `_orders_by_internal_id`, reserve `_get_next_tws_order_id`, `ibkr_contract_from_resolved(resolved)`, `_build_ibkr_order`, stamp `timestamps.ibkr_submit_started_at`, register maps + `register_request_id`, log rich line (symbol, secType, conId, account, qty, type, limit), `client.placeOrder(tws_id, contract, ibOrder)`, stamp `ibkr_submit_completed_at`.

**Callers:** `OMSService._submit_leg`.

**Exceptions:** `ConnectionError` if not connected (`status=ERROR`), `ValueError` on duplicate internal_id or bad limit, propagates `placeOrder` exceptions with `status=ERROR`.

---

## 8. `placeOrder` callbacks — `TWSClient` + `IBKRExecutionAdapter` trio

Callbacks are **synchronous** on the ibapi reader thread; adapter acquires `_lock` and either mutates `OMSOrder` or schedules thread-safe future resolution.

- `TWSClient.placeOrder(tws_id, contract, order)` — `EClient` send (ibapi).
- `IBKRExecutionAdapter.on_order_status` (`ibkr_adapter.py:417-484`) — maps `orderStatus` string, updates `filled/remaining/avgPrice`, stamps `order_status_received_at`, `permId`, `apply_mapped_status`, logs, resolves `fill_futures`, emits `BROKER_ACK/FILL/PARTIAL_FILL`.
- `on_open_order` (`521-565`) — applies `orderState.status`.
- `on_exec_details` (`567-664`) — `execId` dedup, `cumQty` vs incremental, `BrokerExecution` upsert + pending commission attach, status promotion, `last_exec_id`, commission totals.
- `on_commission_report` (`670-720`) — attach commission to `BrokerExecution` or store in `_pending_commissions`.
- `on_error` (`728-777`) — map codes: `202→CANCELLED`, `ORDER_REJECTION_CODES{200,201,10147,10148,10243}→REJECTED`, `{399,2109}|2000-2999|10000-11000→` warning (no status change), else `ERROR`. Terminal orders not regressed.

---

## 9. `reclaim_stale_jobs` — `backend/app/db/repositories/signal_repository.py:486`

```python
async def reclaim_stale_jobs(self, max_attempts: int=3) -> dict[str,int]:
```

**Purpose:** Reclaimer: `lease_expires_at < now`:
- `attempt_count>=max_attempts` + active → `DEAD_LETTER` (any active status).
- `PROCESSING` + not dead → `RECOVERY_REQUIRED` (may have placed orders; quarantine, not requeue).
- `CLAIMED` + not dead → `QUEUED` (never executed; safe requeue).

**Callers:** `ExecutionWorkerPool._reclaimer_loop` every 15s.

**Returns:** `{"dead_lettered":int,"quarantined":int,"requeued":int}`.

---

## 10. `heartbeat_lease` — `backend/app/db/repositories/signal_repository.py:464`

```python
async def heartbeat_lease(self, job_id: Any, worker_id:str, lease_duration_sec:float=30.0) -> bool:
```

**Purpose:** `UPDATE signal_jobs SET lease_expires_at=now+lease_duration WHERE job_id==id AND worker_id==owner AND status IN ACTIVE_LEASE_STATUSES`.

**Returns:** `True` if row affected (still owner); `False` means reclaimed elsewhere (caller sets `lease_lost`).

**Callers:** `ExecutionWorkerPool._lease_heartbeat` loop.

---

## 11. `request_contract_details` / `request_contract_details_async` — `backend/app/broker/ibkr/tws_client.py:369,401`

```python
def request_contract_details(self, contract: Any, *, timeout:float=5.0) -> list[Any]:
async def request_contract_details_async(...)->list[Any]:
```

**Purpose:** Synchronous `reqContractDetails(reqId, contract)` + block on `threading.Event` until `contractDetailsEnd` or timeout; returns collected `ContractDetails`. Async wrapper via `to_thread`.

**Returns:** `list[Any]` (empty if not connected or timeout).

**Callers:** `LivePnlService._request_ticks` (qualify contract), tests, `OrderManager` CFD path.

---

## 12. `claim_next_jobs` — `backend/app/db/repositories/signal_repository.py:347`

```python
async def claim_next_jobs(self, worker_id:str, *, limit:int=1, lease_duration_sec:float=30.0) -> list[SignalJobModel]:
```

**Purpose:** `SELECT ... FOR UPDATE SKIP LOCKED` with **trade_id serialization**: exclude jobs whose `trade_id` has any sibling in `ACTIVE_LEASE_STATUSES` (prevents OPEN vs CLOSE race). Also claims either `QUEUED|RECLAIMABLE` or `ACTIVE` with expired `lease_expires_at`. Order `received_at ASC`. Then `UPDATE SET status=CLAIMED, worker_id, claimed_at, lease_expires_at, attempt_count++` for selected ids, returns fresh rows.

**Callers:** `ExecutionWorkerPool._claim_job`.

---

## 13. `update_status` (fenced) — `backend/app/db/repositories/signal_repository.py:421`

```python
async def update_status(self, job_id, status:str, *, error:str|None, worker_id:str|None, fence:bool, lease_duration_sec:float) -> int:
```

**Purpose:** The fenced write: when `fence=True` adds `WHERE worker_id==owner AND status IN ACTIVE_LEASE_STATUSES`; extending lease on `PROCESSING`; setting `processing_started_at`/`completed_at`; returns `rowcount` (0 means lost lease).

**Callers:** `ExecutionWorkerPool._write_status` (PROCESSING/COMPLETED/REJECTED/FAILED), `RecoveryManager` (RECOVERY_REQUIRED/QUEUED/DEAD_LETTER without fence).

---

## 14. `acquire / mark_executed / release / reconcile_stale_claims` — `backend/app/db/repositories/execution_claim_repository.py:41,118,135,169`

**`acquire(dedupe_key, account_id, strategy_id, signal_id, action, correlation_id, stale_after_sec=300)`** — `INSERT ... ON CONFLICT DO UPDATE WHERE state==ABANDONED` (reclaims only ABANDONED); if no row returned, diagnose existing: `EXECUTED→DuplicateExecutionError`, `CLAIMED` + young→`ExecutionInFlightError`, stale→`ClaimNeedsReconciliationError`.

**`mark_executed(dedupe_key)`** — `UPDATE SET state=EXECUTED WHERE state==CLAIMED`.

**`release(dedupe_key)`** — `UPDATE SET state=ABANDONED WHERE state==CLAIMED` (only when known no orders emitted).

**`reconcile_stale_claims(stale_after_sec=300)`** — for each `CLAIMED` older than cutoff, `count_orders_emitted` → sealed if any orders else released; returns `{released,sealed}`.

**Callers:** `OrderManager._acquire_execution_claim/_seal_execution_claim/_resolve_failed_claim`, `WorkerPool._reclaimer_loop`, `RecoveryManager.run_startup_recovery`.

---

## 15. `size_open` — `backend/app/services/model_blue/sizer.py:55`

See `ModelBlueSizer` in `classes.md` #13. Computes floor-to-share quantities; errors on missing weight/price/invalid direction.

---

## 16. `build_intent` — `backend/app/services/model_blue/strategy.py:75`

See `ModelBlueStrategy` #12. Dispatch OPEN/CLOSE; OPEN sizes, CLOSE reverses legs from `TradeBook`.

---

## 17. `execute` (basket) — `backend/app/oms/coordinator.py:122`

See `BasketCoordinator` #6. The N-leg orchestrator with retry/compensation.

---

## 18. `run_startup_recovery` — `backend/app/services/recovery.py:35`

See `RecoveryManager` #10.

---

## 19. `_verify_webhook_authentication` — `backend/app/api/routes/webhooks.py:144`

```python
def _verify_webhook_authentication(request: Request) -> None:
```

**Purpose:** Enforce `X-Webhook-Secret` **before** DB. If `webhook_auth_enabled` false, pass. Else `hmac.compare_digest(expected, incoming)`; missing/mismatch → `HTTPException 401`. Called at top of `receive_tradingview_webhook`.

---

## 20. `_verify_emergency_killswitch_auth` — `backend/app/api/routes/emergency.py:28`

```python
def _verify_emergency_killswitch_auth(request: Request) -> None:
```

**Purpose:** Enforce `Authorization: Bearer <secret>` before DB. Fail-closed 401 if `EMERGENCY_KILLSWITCH_AUTH_SECRET` empty while enabled. Constant-time compare; never logs secret.

---

## 21. `hydrate_kill_switch_cache / clear_account_kill_switch / is_account_kill_switch_active` — `backend/app/services/kill_switch.py:60,75,105`

- `is_account_kill_switch_active(account_id)->bool` — read `_KILL_SWITCH_ACTIVE_ACCOUNTS`.
- `hydrate_kill_switch_cache(session_factory)->set[int]` — rebuild from `status IN _ARMED_STATUSES`.
- `clear_account_kill_switch(session_factory,account_id,cleared_by)->int` — `UPDATE SET CLEARED + cleared_at/by WHERE status IN _ARMED_STATUSES`; discard cache.

---

## 22. `resolve_leg / attach_resolved / ibkr_contract_from_resolved` — `backend/app/instruments/resolver.py`

- `resolve_leg(symbol,instrument_type,market,currency,con_id,catalog) -> ResolvedInstrument` — maps `STK/CFD/ETF` + US/EU routing, looks up `InstrumentRecord` from catalog (DB) for conId/exchange overrides, raises `InstrumentResolutionError` if ambiguous/missing.
- `attach_resolved(intent, catalog) -> OrderIntent` — `resolve_leg` per leg, returns new intent with `leg.resolved`.
- `ibkr_contract_from_resolved(resolved)->Contract` — sets `secType/symbol/exchange/currency/conId` for `adapter._build_ibkr_contract`.

---

## 23. `unrealized_leg / unrealized_pair` — `backend/app/services/pnl.py:39,44`

```python
def unrealized_leg(signed_qty: Decimal, entry: Decimal, mark: Decimal) -> Decimal: return signed_qty*(mark-entry)
def unrealized_pair(*, leg_a_signed, leg_a_entry, leg_a_mark, leg_b_signed, ...) -> Decimal|None:
```

**Purpose:** Pure pair PnL: long positive qty, short negative. Require all marks; return None if missing.

**Callers:** `LivePnlService._recompute`.

---

## 24. `fetch_in_flight_accounts` — `backend/app/services/position_reconciler.py:448`

```python
async def fetch_in_flight_accounts(session: AsyncSession) -> set[int]:
```

**Purpose:** `BasketModel.state IN (EXECUTING,UNWINDING)` UNION `SignalJobModel.status==PROCESSING (via account_scope string→int)` → `set[int]`. Used to label `ReconcileDiff.in_flight` so UI doesn't alert on legitimately in-flight drift.

---

## 25. `build_ledger_net_lines / classify_reconcile_diffs` — `backend/app/services/position_reconciler.py:87,128`

- `build_ledger_net_lines(open_rows, instruments)->list[LedgerNetLine]` — nets OPEN `positions` per `(account_id,symbol,sec_type)`, ignoring zero nets, with conId set from `InstrumentModel.trade_conid` where available.
- `classify_reconcile_diffs(broker_lines, ledger_lines, ibkr_to_account, timed_out, in_flight_accounts)->list[ReconcileDiff]` — merges by `(account_id,symbol,sec_type)`, emits `UNMAPPED_ACCOUNT` first, then per key: `MATCH/QTY_DRIFT/BROKER_ORPHAN/LEDGER_GHOST` (suppress ghosts when `timed_out`). `QtyClose` within `1e-6`.

---

## 26. `collect_reconcile_positions` — `backend/app/services/reconcile_service.py`

```python
async def collect_reconcile_positions(db: AsyncSession, ibkr_account:str|None) -> ReconcilePositionsResponse:
```

**Purpose:** The `GET /reconcile/positions` impl: fetch latest `PositionReconcileRunModel`, all `BrokerPositionModel` snapshot rows, all OPEN `PositionModel` rows, recompute diffs (or reuse last run) → `ReconcilePositionsResponse`.

---

## 27. `collect_system_monitor_data` — `backend/app/services/system_monitor_service.py`

```python
async def collect_system_monitor_data(session, tws_client, redis_client) -> SystemMonitorResponse:
```

**Purpose:** The `GET /system-monitor` impl: sample `psutil` (CPU/memory/disk/processes), read host info, probe `postgresql` (`SELECT 1`), `redis.ping`, `tws_client.is_connected`, demo port, IB gateway port, build `ServicesHealth`, compute `overall_status` + `alerts`.

---

## 28. `persist_signal_id_for` — `backend/app/db/repositories/signal_repository.py:33`

```python
def persist_signal_id_for(signal: Signal) -> str:
```

**Purpose:** The stable `signals.signal_id` idempotent key: `trade_id` for OPEN, `"{trade_id}:CLOSE"` for CLOSE (unless already suffixed). Mirrors webhook side's `compute_idempotency_key` but from the `Signal` object. Returns empty string if neither field present (caller then warns).

**Callers:** `OrderManager._persist_inbound_signal`.

---

## 29. `execution_dedupe_key` — `backend/app/db/repositories/execution_claim_repository.py:207`

```python
def execution_dedupe_key(intent: OrderIntent) -> str:
```

**Purpose:** Stable barrier key for `execution_claims`: `f"{account_id_or_-}:{strategy_id}:{signal_id}"` (flattens `account_id` to `"-"` when null so unique index works regardless of `NULL`).

**Callers:** `OrderManager._acquire_execution_claim`.

---

## 30. `_save_raw_capture_file / _append_incoming_signal_csv` — `backend/app/api/routes/webhooks.py:59,127`

**Purpose:** Disk side-effects of webhook: write JSON capture to `data/tradingview_webhooks/{req_id}.json`; append CSV row (thread-locked) with flattened leg csv fields + raw JSON. The `WEBHOOK_CAPTURE_DIR = Path(...).parents[3]/"data/tradingview_webhooks"` path is relative to `app/api/routes/webhooks.py`. CSV fields at `webhooks.py:33-56`.

---

## 31. `create_demo_app / _serve / run` — `backend/demo_streaming/api.py:45` & `main.py:30,99`

- `create_demo_app(session_factory, redis, stream_name, trading_api_url, shutdown) -> FastAPI` — builds the standalone demo `FastAPI` described in `api.md` §9, mounts `/assets`, defines `GET /health, /demo/*, /demo/stream, /api/v1/{full_path} proxy, SPA`.
- `async _serve()` / `run()` (`demo_streaming/main.py`) — the `python -m demo_streaming` entry: create engine, `Redis.from_url`, `PositionStream`, `PositionBridge`, `create_demo_app`, `uvicorn.Server(host, port)` + bridge task + shutdown watch.

---

## 32. Miscellaneous small but important functions

| Name | File | Purpose |
|---|---|---|
| `setup_logging` | `backend/app/core/logger.py:18` | Configure `TimedRotatingFileHandler` (`DatedTimedRotatingFileHandler`) daily rollover `storage/logs/trading-YYYY-MM-DD.log`, add `TraceContextFilter` (request_id/signal_id/trade_id) |
| `bind_log_context / clear_log_context` | `backend/app/core/logger.py:61,72` | Thread-local `contextvars` for request correlation |
| `get_settings` | `backend/app/core/config.py:38` | Cached `Settings` (pydantic `BaseSettings`, `extra="ignore"` — unknown env keys ignored, so no `BROKER_MODE`) |
| `paper_retry_ports_allowed` | `backend/app/oms/retry_policy.py:32` | `ibkr_port in {7497,4002}` → paper basket retries allowed, else false |
| `default_paper_retry_policy` | `backend/app/oms/retry_policy.py:42` | returns `ExecutionRetryPolicy(enabled=True,square_off_after_sec=30,max_retries=3,retry_interval=5,retry_window=30)` |
| `get_default_checks` | `backend/app/rms/engine.py:18` | Factory for 5 checks in order 2/3/4/7/8 |
| `normalize_strategy_id / normalize_trade_id` | `backend/app/core/identifiers.py:8,18` | Lowercase/trim + strip prefix noise; used by `compute_idempotency_key` |
| `position_leg_payloads` | `backend/demo_streaming/snapshot.py:42` | Convert one `PositionModel` (+ account + baskets + orders) into 1-2 leg dicts for demo JSON (status, pnl, fills, commissions, etc.) |
| `load_position_rows / load_signals / load_baskets / load_orders` | `backend/demo_streaming/snapshot.py:12,80,110,140` | Demo read helpers (SQLAlchemy selects) |
| `request_positions / request_positions_async` | `backend/app/broker/ibkr/tws_client.py:407,451` | Described in #11; used by reconciler |

---

# PART VI — SAFETY


---

## 14. Kill Switch

> **Source file:** `docs/safety/kill-switch.md`  —  original heading: *Kill Switch — EC2 Emergency Flatten*

**Verified from:** `backend/app/services/kill_switch.py:142`, `backend/app/db/models/kill_switch.py:1`, `backend/app/api/routes/config.py:151`, `backend/app/services/order_manager.py:452`, `backend/app/oms/coordinator.py:83`, `backend/app/rms/models.py:21`, `backend/app/main.py:30`

Operator subsystem that flattens all `OPEN` positions for one `account_id`, arms a durable block on future `OPEN` signals until explicit operator clear, and reconciles the ledger against the broker/order store. Distinct from `accounts.enabled` and allocation flags.

---

## Globals — `backend/app/services/kill_switch.py:40`

```python
_KILL_SWITCH_ACTIVE_ACCOUNTS: set[int] = set()  # line 45 — in-memory read cache; never mutate directly
_ARMED_STATUSES = (                               # line 49 — statuses that keep account armed
    KILL_SWITCH_STATUS_ACTIVATING,                # "ACTIVATING"
    KILL_SWITCH_STATUS_FLATTENING,                # "FLATTENING"
    KILL_SWITCH_STATUS_RECONCILING,               # "RECONCILING"
    KILL_SWITCH_STATUS_RETRYING,                  # "RETRYING"
    KILL_SWITCH_STATUS_FLAT,                      # "FLAT"
    KILL_SWITCH_STATUS_COMPLETE,                  # "COMPLETE"
    KILL_SWITCH_STATUS_UNRESOLVED,                # "UNRESOLVED"
)
# Only KILL_SWITCH_STATUS_CLEARED ("CLEARED") is terminal-and-disarmed.
```

`_KILL_SWITCH_ACTIVE_ACCOUNTS` is a **read cache only**. The authoritative source is the `kill_switch_operations` table. Every mutation must go through the helpers/service so DB and cache stay in step.

## Free Functions — `backend/app/services/kill_switch.py:60`

| Function | Line | Signature | Semantics |
|----------|------|-----------|-----------|
| `is_account_kill_switch_active` | 60 | `(account_id: int) -> bool` | `return account_id in _KILL_SWITCH_ACTIVE_ACCOUNTS` — hot-path check used in `OrderManager._fanout_single_account` |
| `_arm_kill_switch_cache` | 65 | `(account_id: int) -> None` | `set.add(account_id)` — called **inside** the `session.begin()` that inserts the operation row so DB write and cache arm are adjacent; never call outside service |
| `clear_account_kill_switch_cache` | 70 | `(account_id: int) -> None` | `set.discard(account_id)` — in-memory only; prefer the durable `clear_account_kill_switch` |
| `hydrate_kill_switch_cache` | 75 | `(session_factory) -> set[int]` | `SELECT DISTINCT account_id WHERE status IN (_ARMED_STATUSES)` → `clear()` + `update(armed)`; logs `KILL SWITCH REARMED FROM DB` or `no accounts armed`. **Must run before any signal is processed** — invoked via `OrderManager.hydrate_runtime_from_db()` at `backend/app/main.py:65` |
| `clear_account_kill_switch` | 105 | `(session_factory, account_id, *, cleared_by="operator") -> int` | Durable disarm. `UPDATE kill_switch_operations SET status='CLEARED', cleared_at=now, cleared_by=... WHERE account_id=? AND status IN (_ARMED_STATUSES)` inside `session.begin()`, then `discard` cache. Returns `rowcount`. DB write happens **first** — if it fails account stays blocked (safe direction). |

### Cache vs DB ordering invariant

- **Arm:** `initiate_square_off` / `arm_account_kill_switch_only` do `session.add(operation)` → `_arm_kill_switch_cache(account_id)` **inside** the same `session.begin()` transaction before commit (`kill_switch.py:191`, `286`). On startup, `hydrate_kill_switch_cache` rebuilds the set from DB, fixing the pre-hydrate bug where restart silently disarmed.
- **Clear:** `clear_account_kill_switch` writes DB first (`kill_switch.py:118`), then discards cache (`kill_switch.py:132`). Inverse ordering on clear ensures crash-before-discard leaves account still blocked.
- **Delete account:** `backend/app/api/routes/config.py:492` discards `_KILL_SWITCH_ACTIVE_ACCOUNTS` on `DELETE /accounts/{id}`.

---

## Class — `backend/app/services/kill_switch.py:142`

```python
class KillSwitchService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        order_manager: Any | None = None,
        max_concurrent_positions: int = 5,  # line 149
    ) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent_positions)  # line 154
```

### `initiate_square_off` — `kill_switch.py:156`

```python
async def initiate_square_off(self, account_id: int, requested_by: str = "operator"
) -> tuple[KillSwitchOperationModel, bool]:
```

1. `session.get(AccountModel, account_id)` — `ValueError` if not found.
2. Idempotency guard: `SELECT ... WHERE account_id=? AND status IN (ACTIVATING, FLATTENING, RECONCILING, RETRYING)` — if row exists return `(existing_op, False)` (`kill_switch.py:170`).
3. `SELECT PositionModel WHERE account_id=? AND risk_state='OPEN'` → `open_positions`.
4. Insert `KillSwitchOperationModel(operation_id=uuid4(), status=ACTIVATING, requested_by, initial_position_count=len(open_positions), ...)` and `_arm_kill_switch_cache(account_id)` inside `session.begin()`.
5. Returns `(operation, True)` on create; caller `backend/app/api/routes/config.py:178` triggers background flatten only when `created_new is True`.

Note: this guard checks 4 statuses, not the full 7 in `_ARMED_STATUSES`. Accounts with `FLAT/COMPLETE/UNRESOLVED` will create a new operation on next square-off.

### `arm_account_kill_switch_only` — `kill_switch.py:229`

```python
async def arm_account_kill_switch_only(self, account_id: int, requested_by: str = "emergency_webhook"
) -> tuple[KillSwitchOperationModel, bool]:
```

EC2 emergency webhook path. **Does NOT submit IBKR orders.** Differences from `initiate_square_off`:

- Idempotency guard uses **full** `_ARMED_STATUSES` (`kill_switch.py:245`) plus `or is_account_kill_switch_active(account_id)` — if cache already armed but DB row was cleaned, it re-arms cache and returns existing row if present.
- Called by `backend/app/api/routes/emergency.py:127` with `requested_by="emergency_webhook"`.
- Still inserts `ACTIVATING` row and arms cache when no active operation exists; flatten is intentionally skipped.

### `execute_flatten_operation_background` — `kill_switch.py:297`

```python
async def execute_flatten_operation_background(self, operation_id: UUID) -> None:
    asyncio.create_task(self._execute_flatten_operation(operation_id), name=f"kill-switch-flatten-{operation_id}")
```

Non-blocking. HTTP handler returns 202 before flatten starts. No await, no result propagation.

### `_execute_flatten_operation` — `kill_switch.py:304`

Durable off-request-thread worker:

1. `session.get(KillSwitchOperationModel, operation_id)` — early return if `None` or `status in (COMPLETE, UNRESOLVED)` (`kill_switch.py:310`).
2. `op.status = FLATTENING` inside `session.begin()`, reload `OPEN` positions for `op.account_id`.
3. If `not open_positions` → `_update_operation_completion(..., COMPLETE, unresolved=0)` and return.
4. Resolve `baskets_coord = getattr(self._order_manager, "_baskets", None)`.
5. Emit `KILL_SWITCH_ACTIVATED` via `baskets_coord._event` with `{operation_id, account_id, total_positions}` (`kill_switch.py:330`).
6. `tasks = [self._flatten_single_position(...) for pos in open_positions]` → `await asyncio.gather(*tasks, return_exceptions=True)` — bounded only by semaphore, not by `max_concurrent_positions` count of tasks (all tasks created, semaphore gates OMS submit).
7. `await self._reconcile_and_finalize(operation_id, account_id, results)`.

### `_flatten_single_position` — `kill_switch.py:349`

```python
async def _flatten_single_position(self, account_id, ibkr_account, pos: PositionModel, baskets_coord) -> bool:
    async with self._semaphore:  # line 357 — max 5 concurrent positions
```

- Reverse legs from `pos.leg_a_signed_qty` / `pos.leg_b_signed_qty` (`kill_switch.py:361`, `378`):
  ```python
  side = RMSOrderSide.SELL if pos.leg_a_signed_qty > 0 else RMSOrderSide.BUY
  qty  = abs(pos.leg_a_signed_qty)
  ```
  Same for leg B. `Decimal(0)` placeholder price. `contract_month="202612"` hardcoded, `instrument_type=pos.leg_a_instrument_type or "STK"`, `leg_index=0/1`.
- Skip legs with `None` or `abs(qty)==0`; if `not legs: return True` (`kill_switch.py:393`).
- Build `OrderIntent`:
  ```python
  OrderIntent(
      signal_id=f"KILLSWITCH-{pos.trade_id}-{uuid4().hex[:6]}",
      strategy_id=pos.strategy_id,
      action=OrderAction.CLOSE,
      legs=legs,
      account_id=account_id,
      ibkr_account=ibkr_account,
      intent_mode=ExecutionIntentMode.EMERGENCY_FLATTEN,  # line 402
  )
  ```
- If `baskets_coord` present: `await self._order_manager._resolve_instruments(close_intent)` for CFD/STK resolution, then synthetic RMS pass:
  ```python
  RMSResult(outcome=RMSOutcome.PASS, intent=close_intent, original_intent=close_intent,
            reason="KILL_SWITCH_EMERGENCY_CLOSE")  # line 416
  ```
  → `await baskets_coord.execute(close_intent, rms_pass, order_type="MARKET")`.
- Success check: `success and orders`, then `is_fully_filled = all(o.status == OMSOrderStatus.FILLED for o in fill_orders where not is_compensation)` (`kill_switch.py:424`).
- On full fill, atomically:
  ```python
  _exit_marks_from_orders(fill_orders)  # persistence helper
  _commission_from_orders(fill_orders)
  await pos_repo.close_trade(trade_id, account_id, exit_marks, commission)
  await EventRepository.append(process="position", kind="POSITION_CLOSE",
      detail={account_id, trade_id, source="KILL_SWITCH"},
      idempotency_key=f"position_close:kill_switch:{account_id}:{trade_id}")
  ```
  Returns `success` bool; exceptions logged and return `False` (`kill_switch.py:465`).

If `baskets_coord is None` (no OMS wired, tests), returns `True` without submission.

### `_reconcile_and_finalize` — `kill_switch.py:469`

Authoritative reconciliation before completing operation:

1. **Tier-2 auto-repair** inside `session.begin()` (`kill_switch.py:474`):
   ```python
   open_positions = await PositionRepository(session).list_open()
   account_open = [p for p in open_positions if p.account_id == account_id]
   for pos in account_open:
       pos_orders = await OrderRepository(session).list_by_trade_id(pos.trade_id)
       close_orders = [o for o in pos_orders if "KILLSWITCH-" in (o.internal_order_id or "") or ":CLOSE" in (o.internal_order_id or "")]
       filled_close = [o for o in close_orders if o.status == "FILLED"]
       req_legs = 2 if pos.leg_b_symbol else 1
       if len(filled_close) >= req_legs:
           await pos_repo.close_trade(pos.trade_id, account_id, exit_marks={symbol: fill_price})
   ```
2. Count remaining: `account_remaining = [p for p in remaining_positions if account_id==]` (`kill_switch.py:507`).
3. `final_status = COMPLETE if net_unresolved==0 else UNRESOLVED` (`kill_switch.py:510`).
4. `await self._update_operation_completion(operation_id, final_status, unresolved=net_unresolved)`.
5. Emit `KILL_SWITCH_COMPLETED` or `KILL_SWITCH_UNRESOLVED` via `baskets_coord._event` (`kill_switch.py:518`).

### `_update_operation_completion` — `kill_switch.py:536`

```python
async def _update_operation_completion(self, operation_id: UUID, final_status: str, unresolved: int) -> None:
    async with session_factory() as session, session.begin():
        op = await session.get(KillSwitchOperationModel, operation_id)
        op.status = final_status
        op.unresolved_count = unresolved
        op.flattened_count = max(0, op.initial_position_count - unresolved)
        op.updated_at = datetime.now(UTC)
```

Does **not** clear `cleared_at/cleared_by` — operation stays armed (COMPLETE/UNRESOLVED are in `_ARMED_STATUSES`).

---

## Persistence — `backend/app/db/models/kill_switch.py:26`

Table `kill_switch_operations` (`KillSwitchOperationModel`):

| Column | Type | Notes |
|--------|------|-------|
| `operation_id` | `UUID(as_uuid=True) PK` | `default=uuid4`, `server_default=gen_random_uuid()` |
| `account_id` | `BigInteger FK accounts.id` | `index=True` |
| `ibkr_account` | `String` | Denormalized from `AccountModel.ibkr_account` at creation |
| `status` | `String index` | One of 8 constants below |
| `requested_by` | `String` | `"operator"` or `"emergency_webhook"` |
| `initial_position_count` | `BigInteger` | `len(OPEN)` at operation creation |
| `flattened_count` | `BigInteger` | Computed at completion |
| `working_count / retrying_count / unresolved_count` | `BigInteger` | `unresolved_count` is authoritative; others unused in current flow |
| `final_exposure` | `Float` | Always `0.0` in current code |
| `last_error` | `String nullable` | Not set in normal path |
| `cleared_at / cleared_by` | `DateTime(tz) / String nullable` | Set only by `clear_account_kill_switch` |
| `created_at / updated_at` | `DateTime(tz)` | `server_default=now()`, `onupdate=now()` |

**Status constants** (`kill_switch.py:13`):

```
IDLE, ACTIVATING, FLATTENING, RECONCILING, RETRYING, FLAT, COMPLETE, UNRESOLVED, CLEARED
```

Only `CLEARED` disarms. Alembic revision `b6d8f0a2c147` adds `cleared_at`, `cleared_by`.

---

## Account Isolation

- `kill_switch_operations.account_id` + `_KILL_SWITCH_ACTIVE_ACCOUNTS` are per-`account_id`.
- `_fanout_single_account` blocks `OPEN` only for the armed `account_id`; other accounts' fanout proceeds.
- `hydrate_kill_switch_cache` loads `DISTINCT account_id` so one armed account does not block others.
- Tests verify: arming `id_a` leaves `is_account_kill_switch_active(id_b) is False` (`tests/test_emergency_kill_switch.py:258`).

---

## Blocking Behavior

### `OrderManager` gate — `backend/app/services/order_manager.py:452`

```python
from app.services.kill_switch import is_account_kill_switch_active
if intent.action == OrderAction.OPEN and is_account_kill_switch_active(ctx.account_id):
    logger.warning("KILL_SWITCH_ACTIVE: Blocking NEW open signal for account_id=%s ...")
    raise ValueError(f"KILL_SWITCH_ACTIVE: Account {ctx.account_id} is in active emergency kill-switch mode.")
```

- Evaluated in `_fanout_single_account`, **before** `_evaluate_and_submit` (RMS/OMS).
- Only `OPEN` is blocked. `CLOSE` and `EMERGENCY_FLATTEN` pass through.
- Propagates as `AccountExecutionOutcome(error="KILL_SWITCH_ACTIVE: ...")` via `asyncio.gather` in `_fanout_accounts`; `all_rejected` handling marks webhook as `REJECTED`.

### `BasketCoordinator` gate — `backend/app/oms/coordinator.py:83`

```python
def is_open_blocked(self, account_id, strategy_id) -> bool:
    return (account_id, strategy_id) in self._critical  # CRITICAL baskets
```

Separate from kill switch. Kill switch uses `is_account_kill_switch_active`; basket CRITICAL blocks via `is_open_blocked`. Kill-switch flatten itself uses `EMERGENCY_FLATTEN` which bypasses RMS checks 3 and 8, and does not consult either gate for its own CLOSE legs.

---

## Start Again (Clear) — `backend/app/api/routes/config.py:194`

`POST /api/v1/config/accounts/{account_id}/kill-switch/clear` → `clear_account_kill_switch_endpoint:219`

- Validates account exists via `AccountStrategyConfigService.get_account`.
- `cleared = await clear_account_kill_switch(session_factory, account_id, cleared_by="operator")` (`config.py:226`).
- Returns `KillSwitchClearResponse(account_id, ibkr_account, operations_cleared=cleared, kill_switch_active=is_account_kill_switch_active(account_id))`.
- This is the **only** way to resume OPENs. Completing flatten (`COMPLETE`/`UNRESOLVED`) deliberately does not disarm.

Restart without hydrate would silently disarm — `hydrate_kill_switch_cache` in `OrderManager.hydrate_runtime_from_db` (`order_manager.py:205`) prevents it.

---

## Interaction with OMS / RMS

### OMS — `BasketCoordinator`

- Kill-switch path synthesizes `RMSResult(PASS, reason="KILL_SWITCH_EMERGENCY_CLOSE")` and calls `BasketCoordinator.execute(..., order_type="MARKET")` (`kill_switch.py:417`).
- Orders share the **one** `IBKRExecutionAdapter` with `OrderSubmitPacer(0.2s)` — no emergency reserve (`docs/safety.md:74`). `IBKRExecutionScheduler` P0 exists in `broker/ibkr/scheduler.py` but is **not wired**.
- Flatten emits `KILL_SWITCH_ACTIVATED / COMPLETED / UNRESOLVED` via `baskets_coord._event` → `EventRepository.append(process="basket", ...)`.
- Same TWS socket: if Gateway is down, flatten cannot fail over.

### RMS — `ExecutionIntentMode.EMERGENCY_FLATTEN`

`backend/app/rms/models.py:26` defines `EMERGENCY_FLATTEN`. Two checks explicitly bypass for it:

- **Check 3 Strategy** (`rms/checks/strategy.py:27`): `if action==CLOSE or intent_mode==EMERGENCY_FLATTEN: return PASS` — skips unknown-strategy / missing-config rejects.
- **Check 8 Money per stock** (`rms/checks/money_per_stock.py:30`): same bypass — skips per-symbol budget.
- Kill-switch code does **not** run `RMSEngine.evaluate`; it injects the synthetic PASS. Normal TradingView CLOSE signals still go through full RMS.

---

## API Routes

All under `/api/v1` (`backend/app/api/router.py:11`, `backend/app/main.py:170`).

| Method | Path | Handler | Status | File |
|--------|------|---------|--------|------|
| `POST` | `/api/v1/config/accounts/{account_id}/square-off` | `square_off_account_positions` | `202 Accepted` | `api/routes/config.py:151` |
| `GET` | `/api/v1/config/accounts/{account_id}/kill-switch` | `get_account_kill_switch_status` | `200` | `api/routes/config.py:237` |
| `POST` | `/api/v1/config/accounts/{account_id}/kill-switch/clear` | `clear_account_kill_switch_endpoint` | `200` | `api/routes/config.py:194` |
| `POST` | `/api/v1/emergency-kill-switch` | `emergency_kill_switch_endpoint` | `200` | `api/routes/emergency.py:76` |

### `POST /config/accounts/{id}/square-off` — `config.py:151`

- Validates account exists (404 if not).
- `KillSwitchService(session_factory, order_manager).initiate_square_off(account_id, requested_by="operator")`.
- If `created_new`: `await execute_flatten_operation_background(operation_id)` (fire-and-forget).
- Returns `SquareOffResponse(account_id, ibkr_account, squared_off_count=initial_position_count, trade_ids=[], operation_id, status)` with `squared_off_count = op.initial_position_count`.

Duplicate activation while `ACTIVATING/FLATTENING/RECONCILING/RETRYING` returns existing operation, no new background task.

### `POST /config/accounts/{id}/kill-switch/clear` + `GET .../kill-switch`

See Start Again above. `GET` returns `KillSwitchStatusResponse(account_id, kill_switch_active=is_account_kill_switch_active(...))`.

### Proxied from demo dashboard `:8010`

`demo_streaming` proxies `/api/v1/config/*` to trading app `:8000`.

---

## Semaphore 5

`KillSwitchService.__init__(max_concurrent_positions=5)` → `asyncio.Semaphore(5)` (`kill_switch.py:149`, `154`).

- Gates `_flatten_single_position` only (`kill_switch.py:357`).
- `asyncio.gather(*tasks)` creates N coroutines for N positions; semaphore limits concurrent `BasketCoordinator.execute` calls to 5.
- Combined with `OrderSubmitPacer(0.2s)` on the single TWS socket, actual `placeOrder` throughput is paced globally across all flatten legs plus normal traffic.
- No per-account semaphore: 10 positions on one account still burst 5-at-a-time.

---

## Mermaid — Operation Lifecycle

```mermaid
flowchart LR
    A["POST square-off"] --> B["Armed (ACTIVATING)"]
    B --> C["Flatten N positions<br/>semaphore 5"]
    C --> D{"All FILLED?"}
    D -- yes --> E["COMPLETE (armed)"]
    D -- no --> F["UNRESOLVED (armed)"]
    E --> G["POST .../clear → CLEARED"]
    F --> G
    G -. unblocks .-> H["OPENs resume"]
    B -. blocks .-> H
```

---

## Log Greps

| Grep | Stage |
|------|-------|
| `EMERGENCY KILL SWITCH ACTIVATED` | `initiate_square_off` new operation (`kill_switch.py:220`) |
| `EMERGENCY KILL SWITCH ARMED` | `arm_account_kill_switch_only` new operation (`kill_switch.py:288`) |
| `KILL SWITCH REARMED FROM DB` | `hydrate_kill_switch_cache` on startup |
| `KILL SWITCH CLEARED` | `clear_account_kill_switch` (`kill_switch.py:133`) |
| `KILL_SWITCH_ACTIVE: Blocking NEW open signal` | `order_manager.py:454` fan-out reject |
| `Kill Switch persisted position close` | `_flatten_single_position` full fill (`kill_switch.py:458`) |
| `Reconciled stale position to CLOSED during Kill Switch` | `_reconcile_and_finalize` tier-2 (`kill_switch.py:500`) |
| `Kill Switch operation_id=.* finalized` | `_reconcile_and_finalize` completion (`kill_switch.py:529`) |

## Invariants

1. DB write before cache discard on clear — crash leaves account safely blocked.
2. Never treat `COMPLETE` as disarm — only `CLEARED` allows OPENs.
3. `hydrate_kill_switch_cache` must run before workers process signals (wired in `hydrate_runtime_from_db`).
4. Flatten intents are `EMERGENCY_FLATTEN` CLOSE with synthetic RMS PASS — do not size from webhook payload.
5. CLOSE signals are never blocked by armed cache.
6. Reconciliation checks `OrderRepository.list_by_trade_id` + `status==FILLED` before `close_trade`.

---

## 15. Emergency Kill Switch

> **Source file:** `docs/safety/emergency-kill-switch.md`  —  original heading: *Emergency Kill Switch — LOCAL vs EC2*

**Verified from:** `backend/app/api/routes/emergency.py:1`, `backend/app/services/kill_switch.py:229`, `backend/app/core/config.py:72`, `backend/app/api/routes/config.py:194`, `backend/app/db/models/kill_switch.py:1`, `backend/app/schemas/config_schemas.py:177`, `backend/scripts/oms/flatten_gateway_positions.py:1`

The emergency system is split across two independent surfaces that arm the **same** durable state.

| Surface | Where it runs | What it does | Broker orders |
|---------|---------------|--------------|---------------|
| **EC2** | Trading app FastAPI (`app.main:app` on EC2 `:8000`, route `POST /api/v1/emergency-kill-switch`) | Arms the existing `kill_switch_operations` + `_KILL_SWITCH_ACTIVE_ACCOUNTS` for one `ibkr_account` string | **None** — no OMS/Basket/TWS calls |
| **LOCAL** | Operator workstation/laptop (`backend/scripts/oms/flatten_gateway_positions.py`, CLI) | Talks directly to IBKR TWS/Gateway `reqPositions` → `placeOrder` MARKET closes | **Yes** — paced MARKET reverses on the Gateway snapshot |

If EC2 is unreachable, LOCAL proceeds with its own broker flatten directly. EC2 never submits broker orders; LOCAL never writes `kill_switch_operations` (caller should arm EC2 first if both are needed).

---

## EC2 — Pre-Flight Webhook

### Route — `backend/app/api/routes/emergency.py:76`

```python
router = APIRouter(prefix="", tags=["emergency"])  # included via api_router → /api/v1
@router.post(
    "/emergency-kill-switch",
    response_model=EmergencyKillSwitchResponse,
    status_code=200,
)
async def emergency_kill_switch_endpoint(
    body: EmergencyKillSwitchRequest,  # {"ibkr_account_id": str}
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> EmergencyKillSwitchResponse:
```

Registered in `backend/app/api/router.py:6` and mounted at `backend/app/main.py:170` as `prefix="/api/v1"` → effective path `POST /api/v1/emergency-kill-switch`.

**Execution order** (`emergency.py:91`):

1. Auth → 2. Resolve `ibkr_account` → 3. Arm kill switch → 4. Return 200. No branch submits to IBKR.

### Auth — `backend/app/api/routes/emergency.py:28`

```python
def _verify_emergency_killswitch_auth(request: Request) -> None:
```

Settings (`backend/app/core/config.py:72`):

```python
emergency_killswitch_auth_secret: str | None = None
emergency_killswitch_auth_enabled: bool = True
```

| Condition | Behavior | Log |
|-----------|----------|-----|
| `emergency_killswitch_auth_enabled is False` | **Bypass** — no header check (`emergency.py:35`, `logger.info`) | `Emergency kill switch authentication is disabled` |
| `True` and `emergency_killswitch_auth_secret` is falsy | **Fail closed 401** — `Emergency kill switch authentication not configured` | `logger.warning` |
| Header missing | `401 Missing Authorization header` (`emergency.py:50`) | warning |
| Header not `Bearer <token>` (split len!=2 or scheme != `Bearer`) | `401 Malformed Authorization header. Expected Bearer token.` (`emergency.py:58`) | warning |
| `hmac.compare_digest(expected.encode(), incoming.encode()) is False` | `401 Invalid emergency authentication secret` (`emergency.py:66`) | warning |
| Valid Bearer | Pass | — |

- Constant-time compare via `hmac.compare_digest` (`emergency.py:66`).
- Secret and `Authorization` header are **never logged**.
- Fails closed when enabled but misconfigured.

### Account Resolution — `backend/app/api/routes/emergency.py:103`

```python
clean_ibkr_id = body.ibkr_account_id.strip()
if not clean_ibkr_id:
    raise HTTPException(400, "ibkr_account_id must be non-empty.")
stmt = select(AccountModel).where(func.upper(AccountModel.ibkr_account) == clean_ibkr_id.upper())
account = (await session.execute(stmt)).scalars().first()
if account is None:
    raise HTTPException(404, f"Account with IBKR identifier '{clean_ibkr_id}' not found.")
```

- **Case-insensitive** via `func.upper` (`emergency.py:103`); whitespace trimmed.
- Resolves `ibkr_account` string → internal `account.id` (integer PK).
- 404 if no `accounts` row matches; 400 if trimmed string empty.

### Arm — `backend/app/api/routes/emergency.py:114`

```python
session_factory = getattr(request.app.state, "session_factory", None) or AsyncSessionLocal
order_manager = getattr(request.app.state, "order_manager", None)
kill_switch_svc = KillSwitchService(session_factory=session_factory, order_manager=order_manager)
_op, created_new = await kill_switch_svc.arm_account_kill_switch_only(
    account_id=account.id, requested_by="emergency_webhook")
```

Delegates to `backend/app/services/kill_switch.py:229` `arm_account_kill_switch_only`:

- Idempotency guard: `status IN (_ARMED_STATUSES)` (7 statuses) **or** `is_account_kill_switch_active(account_id)` (`kill_switch.py:249`). If either true → `_arm_kill_switch_cache`, return existing `op` if present, else create new after `existing_op is None` check.
- On create: insert `KillSwitchOperationModel(status=ACTIVATING, requested_by="emergency_webhook", initial_position_count=len(OPEN), ...)` and `_arm_kill_switch_cache`.
- **No** `BasketCoordinator.execute`, no `TWSClient.placeOrder`, no OMS call.

### Response — `backend/app/schemas/config_schemas.py:177`

```python
class EmergencyKillSwitchRequest(BaseModel):
    ibkr_account_id: str = Field(..., min_length=1)

class EmergencyKillSwitchResponse(BaseModel):
    success: bool
    ibkr_account_id: str
    kill_switch_active: bool
    message: str
```

- `200 success=True, ibkr_account_id=account.ibkr_account, kill_switch_active=True` always on arm success (`emergency.py:143`).
- `message = "Emergency kill switch activated for account"` if `created_new`, else `"Kill switch was already active for account"` (`emergency.py:138`).
- Arm persists `requested_by="emergency_webhook"` and `status=ACTIVATING` in `kill_switch_operations` (verified in `tests/test_emergency_kill_switch.py:218`).
- `500 "Failed to persist emergency kill switch state."` on any exception from `arm_account_kill_switch_only` (`emergency.py:131`).

### Timeout / Failure

- No broker call → no Gateway timeout on EC2. Failure modes are: 401 auth, 400 empty id, 404 unknown account, 500 DB/service exception.
- DB failure is caught and wrapped as 500 (`emergency.py:131`); kill switch stays disarmed (DB write failed before cache arm — safe).
- Webhook is idempotent: repeated POST with same `ibkr_account_id` returns 200 already-active (no duplicate row).

### Interaction with OMS / RMS

- None. EC2 webhook does not build `OrderIntent`, does not call `RMSResult` or `BasketCoordinator`, and does not trigger `OrderManager.is_account_kill_switch_active` gate itself — it **sets** the gate so future `OPEN` signals in `OrderManager._fanout_single_account` are blocked (`order_manager.py:452`).

### Start Again

Same as kill switch: `POST /api/v1/config/accounts/{account_id}/kill-switch/clear` (`api/routes/config.py:194`) → `clear_account_kill_switch` moves the emergency-armed row to `CLEARED`. Verified in `tests/test_emergency_kill_switch.py:334` that clear resets `is_account_kill_switch_active` after emergency arm.

Normal `POST .../square-off` and `POST /emergency-kill-switch` operate on the **same** `_ARMED_STATUSES` + `_KILL_SWITCH_ACTIVE_ACCOUNTS` state without conflict (`tests/test_emergency_kill_switch.py:361`).

---

## LOCAL — Direct Gateway Flatten

### Script — `backend/scripts/oms/flatten_gateway_positions.py:1`

```
Operator script: flatten live IB Gateway/TWS positions with paced MARKET closes.
Reads open positions via reqPositions, then submits the opposite MARKET order
for each line (BUY to cover shorts, SELL to close longs). Pacing matches
production OrderSubmitPacer (0.2s). Uses API client id 99 by default so it
does not disconnect the trading app (client id 1).

This talks to IBKR directly. It does NOT use the app kill-switch flatten path
(that only closes Postgres OPEN rows). Arm the kill switch first if you need
to block TradingView OPENs while this runs:
    curl -X POST http://127.0.0.1:8000/api/v1/config/accounts/7/square-off
```

### CLI — `flatten_gateway_positions.py:205`

```bash
python backend/scripts/oms/flatten_gateway_positions.py \
  [--host HOST] [--port PORT] [--client-id 99] \
  [--account DUR919062] [--sec-type CFD] \
  [--pace 0.2] [--fill-timeout 90.0] \
  [--apply] [--allow-live]
```

| Flag | Default | Semantics | Code |
|------|---------|-----------|------|
| `--host` | `Settings.ibkr_host` (127.0.0.1) | TWS/Gateway host | `::253` |
| `--port` | `Settings.ibkr_port` (7497) | TWS/Gateway port | `::257` |
| `--client-id` | `99` | API client id; **refuses** `== Settings.ibkr_client_id` (default 1) with exit 2 | `::214`, `::261` |
| `--account` | `DUR919062` | Only flatten this `ibkr_account` string; empty = all accounts | `::220` |
| `--sec-type` | `CFD` | Only flatten this `secType`; `ALL` disables filter | `::224`, `259` |
| `--pace` | `0.2` | Minimum seconds between `placeOrder` calls; matches production `OrderSubmitPacer` | `::229` |
| `--fill-timeout` | `90.0` | Seconds to wait after last submit for terminal statuses | `::234` |
| `--apply` | off | **Dry run by default.** Without flag prints plan and exits 0 | `::316` |
| `--allow-live` | off | Required to submit on ports outside `PAPER_IBKR_PORTS={7497,4002}`; otherwise exit 2 | `::246`, `268` |

Exit codes: `0` success (dry run or all filled), `1` connect/fill/rejected/pending failures or missing `conId`, `2` bad args (client-id clash, live port without `--allow-live`, negative pace).

### Gateway Connection — `flatten_gateway_positions.py:279`

```python
client = TWSClient()
client.register_listener(listener)  # FlattenListener
connected = client.connect_and_start(
    host=host, port=port, client_id=args.client_id,
    timeout=float(settings.ibkr_connection_timeout),  # default 10s, Settings
)
if not connected:
    print("FAILURE: could not connect to TWS/Gateway", file=sys.stderr)
    return 1
...
finally:
    client.disconnect_clean()
```

- Uses `backend/app/broker/ibkr/tws_client.py:TWSClient` — same wrapper as trading app but separate socket/client-id.
- `connect_and_start` handshake provides `nextValidId` for `placeOrder`; `_next_order_id` reads `client.next_order_id` and increments (`flatten_gateway_positions.py:197`).
- Logs prefix `flatten-gateway` via `setup_logging(level="INFO", filename_prefix="flatten-gateway")` (`::254`).

### Position Retrieval — `flatten_gateway_positions.py:77`

```python
client.reqPositions()
listener.wait_positions(timeout=15.0)       # FlattenListener._pos_done Event
client.cancelPositions()
time.sleep(0.2)

class OpenPosition:
    account: str; symbol: str; sec_type: str; con_id: int
    currency: str; quantity: float; avg_cost: float
    @property close_action -> "SELL" if quantity>0 else "BUY"
    @property close_qty -> abs(quantity)

class FlattenListener:
    positions: list[OpenPosition]
    on_position(account, contract, position, avgCost) -> appends if abs(qty)>1e-9
    on_position_end() -> _pos_done.set()
    on_order_status(orderId, status, filled, remaining, avgFillPrice, ...) -> tracks submitted
    on_error(reqId, errorCode, errorString) -> ignores 2000-2999 warnings; maps 201→Rejected
    all_terminal() -> all status in {"Filled","Cancelled","ApiCancelled","Inactive","Rejected"}
```

- Filter: `_filter_positions(rows, account=..., sec_type=...)` exact `account` match, `sec_type.upper()` match, sorted by `(account, symbol, con_id)` (`::169`).
- Plan printed: `ACCOUNT SYMBOL TYPE QTY ACTION CLOSE QTY CONID` (`::187`).
- Refuses to submit if any `con_id==0`: `missing_conid` → `return 1` with `Refusing to submit: missing conId` (`::308`).
- `wait_positions` timeout is **15.0s** hardcoded (`::295`); warning `positionEnd timed out` but uses whatever arrived.

### Closing Orders — `flatten_gateway_positions.py:147`

```python
def _build_close_contract(pos: OpenPosition) -> Contract:
    contract.conId = pos.con_id  # if truthy
    contract.symbol = pos.symbol
    contract.secType = pos.sec_type or "CFD"
    contract.exchange = "SMART"
    contract.currency = pos.currency or "USD"
    return contract

def _build_close_order(pos: OpenPosition) -> IBOrder:
    order.action = pos.close_action   # "SELL"/"BUY"
    order.totalQuantity = pos.close_qty
    order.orderType = "MKT"
    order.transmit = True
    order.eTradeOnly = False; order.firmQuoteOnly = False
    order.account = pos.account
    return order
```

Paced submit loop (`::322`):

```python
for pos in plan:
    wait = args.pace - (time.monotonic() - last_submit)
    if wait > 0: time.sleep(wait)
    tws_id = _next_order_id(client)
    listener.submitted[tws_id] = SubmittedClose(tws_id, pos, remaining=pos.close_qty)
    client.placeOrder(tws_id, _build_close_contract(pos), _build_close_order(pos))
    last_submit = time.monotonic()
```

- One `MKT` order per snapshot line; no instrument discovery, no `EMERGENCY_FLATTEN` mode (script is outside the app).
- Orders are **not** tagged to `baskets`, `orders`, or `kill_switch_operations` — purely broker-side.

### Verification — `flatten_gateway_positions.py:338`

```python
deadline = time.monotonic() + args.fill_timeout
while time.monotonic() < deadline and not listener.all_terminal():
    time.sleep(0.25)
# Then:
for row in listener.submitted.values():
    if row.status == "Filled": filled+=1
    elif row.status in {"Rejected","Inactive","Cancelled","ApiCancelled"}: rejected+=1
    else: pending+=1
print(f"Summary: submitted={len(plan)} filled={filled} rejected/inactive={rejected} pending={pending}")
return 0 if pending==0 and rejected==0 else 1
```

- Polls `on_order_status` + `on_error` until all terminal (`Filled/Cancelled/ApiCancelled/Inactive/Rejected`) or `fill_timeout` expiry.
- Returns `1` if any `pending` or `rejected`; `0` only when all `Filled`.
- No DB reconciliation: does not update `positions` ledger, does not clear kill switch. Operator must run repair/verify via `GET /api/v1/reconcile/positions` or restart app to reconcile.

### CSV Audit — Not Verified

`flatten_gateway_positions.py` **does not write a CSV audit file**. It prints the plan and `Results:`/`Summary:` lines to stdout only. No CSV path is referenced in code. If a CSV is produced by another LOCAL tool or wrapper, it is not in this script — document as **NOT verified** for this script. The app's kill-switch path logs `EventRepository` rows (`KILL_SWITCH_ACTIVATED/COMPLETED/UNRESOLVED`, `POSITION_CLOSE`, `BROKER_ACK/FILL`) and `storage/logs/flatten-gateway-YYYY-MM-DD.log` via `setup_logging`, which are the auditable artifacts for LOCAL runs.

### Paper vs Live Safety

- `PAPER_IBKR_PORTS = {7497, 4002}` (`app/oms/retry_policy.py:PAPER_IBKR_PORTS`, checked in `flatten_gateway_positions.py:39`, `268`).
- Without `--allow-live`, submitting on any other port exits 2 with `Refusing port ... not a paper port`.
- With `--apply --allow-live --port 7496`, live ports are allowed — no further guard.

---

## Mermaid — LOCAL vs EC2

```mermaid
sequenceDiagram
    participant OP as Operator
    participant EC2 as EC2 :8000
    participant DB as Postgres
    participant LOC as Local Script
    participant GW as Gateway

    OP->>EC2: POST /emergency-kill-switch (Bearer)
    EC2->>DB: arm kill switch (no orders)
    EC2-->>OP: 200 armed

    OP->>LOC: flatten --account X --apply
    LOC->>GW: reqPositions → plan
    LOC->>GW: MKT close per position (0.2s)
    GW-->>LOC: Filled
    LOC-->>OP: done

    Note over OP,DB: Combined: arm EC2 → flatten Local → clear when safe
```

### Combined Flow (Operator Runbook)

1. **Arm EC2** first if reachable: `curl -H "Authorization: Bearer $EMERGENCY_KILLSWITCH_AUTH_SECRET" -H "Content-Type: application/json" -d '{"ibkr_account_id":"DUR919062"}' https://ec2-host/api/v1/emergency-kill-switch`. If EC2 unreachable, proceed to step 2 anyway.
2. **Flatten LOCAL**: `python backend/scripts/oms/flatten_gateway_positions.py --account DUR919062 --sec-type CFD --apply` (dry-run without `--apply` first). Use `--allow-live` only on live ports.
3. **Verify**: re-run without `--apply` should show `Nothing to flatten`; otherwise `GET /api/v1/reconcile/positions?ibkr_account=DUR919062` diffs `broker_positions` vs `positions`.
4. **Reconcile ledger**: app `PositionReconciler` + kill-switch `Tier-2` will auto-close stale OPEN rows whose orders filled; else `scripts/repair_historical_killswitch_positions.py`.
5. **Start Again**: `POST /api/v1/config/accounts/{id}/kill-switch/clear` when safe — only this resumes OPENs.

---

## Account Resolution Details

| Surface | Input | Normalization | Lookup | Failure |
|---------|-------|---------------|--------|---------|
| EC2 webhook | `ibkr_account_id` string | `.strip()` + `upper()` (`emergency.py:95`, `103`) | `func.upper(AccountModel.ibkr_account) == clean.upper()` | `400` empty, `404` not found |
| LOCAL script | `--account DUR919062` | `.strip()` or `None` if empty (`::258`); `sec_type.upper()` else `"CFD"` | Broker snapshot filter `row.account == account` exact match (`::178`); EC2 arm not required for LOCAL to run | No DB lookup — filters snapshot only; wrong account string just yields empty plan → `Nothing to flatten` exit 0 |

Case-insensitive only on EC2; LOCAL filters are case-sensitive on `account` and case-normalized on `sec_type`.

---

## What Is NOT Verified / NOT Built

- **LOCAL CSV audit file**: `flatten_gateway_positions.py` does not write CSV. If an operator wrapper adds CSV export, it is not in this file — flagged as NOT verified.
- **LOCAL kill-switch arming**: LOCAL script does not insert `kill_switch_operations` or arm `_KILL_SWITCH_ACTIVE_ACCOUNTS`. Operator must separately `POST /emergency-kill-switch` or `POST .../square-off` to block new OPENs.
- **EC2 broker flatten**: EC2 webhook never calls `reqPositions` or `placeOrder`. Broker flatten only happens via LOCAL script or via EC2 `POST .../square-off` → `KillSwitchService._execute_flatten_operation` → `BasketCoordinator` (different mechanism from LOCAL's direct Gateway close).
- **Reconnect on Gateway drop**: `TWSClient.disconnect_clean` on LOCAL script; app `IBKRExecutionAdapter` does **not** auto-reconnect on submission — `ConnectionError` surfaces (`docs/safety.md:76`).
- **Per-gateway rate limits / multi-Gateway pool**: Not built — both surfaces share the one `OrderSubmitPacer(0.2s)` or script `--pace 0.2s` on a single socket (`docs/backend-multi-gateway.md`).
- **EC2 `ibkr_account_id` → `conId` mapping**: EC2 webhook resolves to `accounts.id` only; no contract/position lookup.
- **LOCAL `--sec-type ALL` vs `CFD` default**: LOCAL defaults `CFD` only; EC2 flatten closes whatever `positions` ledger rows exist regardless of `secType`.

## Related Docs

- `kill-switch.md` — full `KillSwitchService` globals, methods, persistence, blocking gate, Start Again.
- `backend-kill-switch.md` — operator HTTP API summary (flatten 202, clear, status).
- `backend-multi-gateway.md` — why one pacer/socket is a gap.
- `safety.md` — ports, pacing, STK→CFD override.

---

# PART VII — OPERATIONS


---

## 16. Configuration

> **Source file:** `docs/operations/configuration.md`  —  original heading: *Operations — Configuration*

**Verified from:** `backend/app/core/config.py:10` (`Settings`), `backend/demo_streaming/config.py:7` (`DemoStreamSettings`), `backend/app/db/session.py:16`, `backend/app/main.py:36`, `backend/app/api/routes/webhooks.py:144`, `backend/app/api/routes/emergency.py:28`, `backend/app/instruments/execution_override.py:16`.

Settings load from environment variables and optional `.env` via `pydantic_settings.BaseSettings` with `SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)` — see `backend/app/core/config.py:18` and `backend/demo_streaming/config.py:7`. Unknown keys (including historical `BROKER_MODE`, `ALLOCATIONS_CONFIG_PATH`) are **ignored**. There is no `BROKER_MODE` / MockBroker field. Construction is via `get_settings()` (`backend/app/core/config.py:90`). Do not construct `Settings()` ad hoc.

All `Settings` env names are the uppercased field name (e.g. `app_name` → `APP_NAME`). `DemoStreamSettings` follows the same rule (e.g. `demo_stream_host` → `DEMO_STREAM_HOST`).

Secrets in examples below are placeholders. Replace with vault-managed values.

---

## 1. `Settings` — `backend/app/core/config.py:10`

### Application

| Field | Env | Purpose | Where consumed | Default | Required | Sensitivity | Example |
|-------|-----|---------|----------------|---------|----------|-------------|---------|
| `app_name` | `APP_NAME` | FastAPI title | `backend/app/main.py:149` (`FastAPI(title=settings.app_name)`) | `"IBKR Paper Trading System"` | No | Low | `APP_NAME="IBKR Paper Trading System"` |
| `environment` | `ENVIRONMENT` | Deployment label (development/staging/production) | Stored on `Settings`; not branched in hot path | `"development"` | No | Low | `ENVIRONMENT=production` |
| `log_level` | `LOG_LEVEL` | Root log level passed to `setup_logging` | `backend/app/main.py:37` (`setup_logging(level=settings.log_level)`); also `backend/demo_streaming/main.py` uses `"INFO"` literal for demo process | `"INFO"` | No | Low | `LOG_LEVEL=INFO` |

### Database

| Field | Env | Purpose | Where consumed | Default | Required | Sensitivity | Example |
|-------|-----|---------|----------------|---------|----------|-------------|---------|
| `database_url` | `DATABASE_URL` | Async SQLAlchemy URL (asyncpg) | `backend/app/db/session.py:19` (`create_async_engine(settings.database_url, ...)`); `backend/app/main.py:52` via `AsyncSessionLocal`; `backend/demo_streaming/main.py:33` for demo engine | `postgresql+asyncpg://root:root123@localhost:5433/ibkr_trading` | Yes in production (override default) | **High** — contains password | `DATABASE_URL=postgresql+asyncpg://user:<db-password>@db-host:5432/ibkr_trading` |

Pool defaults are not settings fields — hardcoded in `backend/app/db/session.py:22`: `pool_size=20`, `max_overflow=30`, `pool_timeout=30`, `pool_recycle=1800`, `pool_pre_ping=True`.

### IBKR connection (single TWS/Gateway socket)

There is **one** `TWSClient` / `OrderSubmitPacer` / socket for all accounts. Per-account `ibkr_account` routing tags `ib_order.account` (`backend/app/oms/ibkr_adapter.py:175`) — it does **not** select a host/port. See `docs/backend-multi-gateway.md`.

| Field | Env | Purpose | Where consumed | Default | Required | Sensitivity | Example |
|-------|-----|---------|----------------|---------|----------|-------------|---------|
| `ibkr_host` | `IBKR_HOST` | TWS/Gateway hostname | `backend/app/main.py:47` and `:71` (`IBKRExecutionAdapter` / `client.connect_and_start`) | `"127.0.0.1"` | No (override for non-localhost Gateway) | Low | `IBKR_HOST=127.0.0.1` |
| `ibkr_port` | `IBKR_PORT` | TWS/Gateway port — selects paper vs live retry gating | `backend/app/main.py:48`/`72`; `backend/app/oms/retry_policy.py:10` (`PAPER_IBKR_PORTS={7497,4002}` gates `paper_retries_allowed`); `backend/app/services/order_manager.py:168`/`270` | `7497` (paper TWS) | No | Low | `IBKR_PORT=7497` (paper TWS), `4002` (paper Gateway), `7496`/`4001` (live — retries disabled) |
| `ibkr_client_id` | `IBKR_CLIENT_ID` | Unique API client ID for this socket | `backend/app/main.py:49`/`73`; `backend/app/broker/ibkr/tws_client.py:470` | `1` | No | Low | `IBKR_CLIENT_ID=1` (duplicate IDs on same Gateway disconnect older session) |
| `ibkr_connection_timeout` | `IBKR_CONNECTION_TIMEOUT` | Seconds to wait for `nextValidId` handshake | `backend/app/main.py:47` (`timeout=float(settings.ibkr_connection_timeout)`) | `10` | No | Low | `IBKR_CONNECTION_TIMEOUT=10` |

### IBKR market data

Consumed by live PnL subscription path, not by order submission. `LivePnlService` / `backend/app/broker/ibkr/tws_client.py` contract/market-data wiring reads these via `Settings`.

| Field | Env | Purpose | Where consumed | Default | Required | Sensitivity | Example |
|-------|-----|---------|----------------|---------|----------|-------------|---------|
| `ibkr_market_data_type` | `IBKR_MARKET_DATA_TYPE` | IBKR market data type (1=live, 2=frozen, 3=delayed, 4=delayed-frozen) | Market data subscription stack (`backend/app/services/pnl.py`, `backend/app/broker/ibkr/`) | `3` | No | Low | `IBKR_MARKET_DATA_TYPE=3` |
| `ibkr_market_data_symbol` | `IBKR_MARKET_DATA_SYMBOL` | Default symbol for market-data smoke checks | Same stack | `"AAPL"` | No | Low | `IBKR_MARKET_DATA_SYMBOL=AAPL` |
| `ibkr_market_data_sec_type` | `IBKR_MARKET_DATA_SEC_TYPE` | Default secType for market-data checks | Same stack | `"STK"` | No | Low | `IBKR_MARKET_DATA_SEC_TYPE=STK` |
| `ibkr_market_data_exchange` | `IBKR_MARKET_DATA_EXCHANGE` | Default exchange for market-data checks | Same stack | `"SMART"` | No | Low | `IBKR_MARKET_DATA_EXCHANGE=SMART` |
| `ibkr_market_data_currency` | `IBKR_MARKET_DATA_CURRENCY` | Default currency for market-data checks | Same stack | `"USD"` | No | Low | `IBKR_MARKET_DATA_CURRENCY=USD` |
| `ibkr_market_data_primary_exchange` | `IBKR_MARKET_DATA_PRIMARY_EXCHANGE` | Primary exchange override for market-data contract | Same stack | `None` | No | Low | `IBKR_MARKET_DATA_PRIMARY_EXCHANGE=NASDAQ` |

### Trading (legacy / defaults)

| Field | Env | Purpose | Where consumed | Default | Required | Sensitivity | Example |
|-------|-----|---------|----------------|---------|----------|-------------|---------|
| `trading_symbol` | `TRADING_SYMBOL` | Single-name leg fallback passed to `OrderManager` | `backend/app/main.py:55` (`OrderManager(symbol=settings.trading_symbol)`) — Model Blue webhook path sizes from signal legs, not this universe | `"RELIANCE"` | No | Low | `TRADING_SYMBOL=RELIANCE` |
| `candle_timeframe` | `CANDLE_TIMEFRAME` | Candle string parsed by `candle_timeframe_minutes` | `backend/app/core/config.py:78` property only; **not** used by Model Blue webhook path | `"5 mins"` | No | Low | `CANDLE_TIMEFRAME="5 mins"` |
| `strategy_candle_count` | `STRATEGY_CANDLE_COUNT` | Candle count for non-Model-Blue strategies | Stored only; not used by Model Blue path | `5` | No | Low | `STRATEGY_CANDLE_COUNT=5` |
| `order_quantity` | `ORDER_QUANTITY` | Default quantity arg on `OrderManager` | `backend/app/main.py:56` (`OrderManager(quantity=settings.order_quantity)`) — OPEN uses signal/Model Blue sizing; CLOSE uses open position qty | `1` | No | Low | `ORDER_QUANTITY=1` |

### Model Blue / paper overrides

| Field | Env | Purpose | Where consumed | Default | Required | Sensitivity | Example |
|-------|-----|---------|----------------|---------|----------|-------------|---------|
| `model_blue_committed_notional` | `MODEL_BLUE_COMMITTED_NOTIONAL` | Temporary paper-testing base-leg committed USD for Model Blue OPEN | `backend/app/services/model_blue/allocation.py:27` (`TemporarySettingsCommittedCapitalProvider` — tests only); production uses `backend/app/services/model_blue/db_allocation.py:17` (`DatabaseCommittedCapitalProvider` reads Postgres allocations, does **not** read this env). When `None` and DB capital missing, OPEN is rejected — see `backend/app/services/model_blue/sizer.py:79` | `None` | No — but if `None` and DB has no allocation, OPEN rejects with `MODEL_BLUE_COMMITTED_NOT_CONFIGURED` | Low (paper value, not a fund commitment) | `MODEL_BLUE_COMMITTED_NOTIONAL=100000` (paper only; remove when DB allocations populated) |
| `paper_execute_stk_as_cfd` | `PAPER_EXECUTE_STK_AS_CFD` | Paper/demo: requested STK executes as IBKR CFD; raw `instrument_type` stays STK in DB | `backend/app/instruments/execution_override.py:19` (`paper_execute_stk_as_cfd_enabled()`); `backend/app/services/order_manager.py:1107` / `backend/app/instruments/resolver.py:157` | `True` | No | Low | `PAPER_EXECUTE_STK_AS_CFD=false` to disable |

### Webhook security

| Field | Env | Purpose | Where consumed | Default | Required | Sensitivity | Example |
|-------|-----|---------|----------------|---------|----------|-------------|---------|
| `webhook_auth_enabled` | `WEBHOOK_AUTH_ENABLED` | Master switch for webhook `X-Webhook-Secret` check | `backend/app/api/routes/webhooks.py:147` (`if not settings.webhook_auth_enabled: return`) | `True` | No | Low | `WEBHOOK_AUTH_ENABLED=true` |
| `webhook_auth_secret` | `WEBHOOK_AUTH_SECRET` | Expected value of `X-Webhook-Secret` header (constant-time compare) | `backend/app/api/routes/webhooks.py:151` (`hmac.compare_digest`) | `None` | Yes when `webhook_auth_enabled=true` (unauthenticated webhooks rejected if set; if `None` and enabled, `X-Webhook-Secret` must still match — missing triggers 401) | **High** — webhook shared secret | `WEBHOOK_AUTH_SECRET=<webhook-shared-secret>` |

Auth behavior: header `X-Webhook-Secret` must equal `webhook_auth_secret` via `hmac.compare_digest`. If `webhook_auth_enabled=false`, auth is bypassed (log only). Do not log the secret.

### Emergency kill switch security

| Field | Env | Purpose | Where consumed | Default | Required | Sensitivity | Example |
|-------|-----|---------|----------------|---------|----------|-------------|---------|
| `emergency_killswitch_auth_enabled` | `EMERGENCY_KILLSWITCH_AUTH_ENABLED` | Master switch for emergency kill switch `Authorization: Bearer` check | `backend/app/api/routes/emergency.py:35` | `True` | No | Low | `EMERGENCY_KILLSWITCH_AUTH_ENABLED=true` |
| `emergency_killswitch_auth_secret` | `EMERGENCY_KILLSWITCH_AUTH_SECRET` | Expected Bearer token for `POST /emergency-kill-switch` | `backend/app/api/routes/emergency.py:39` (`hmac.compare_digest` against `Authorization: Bearer <token>`) — fails closed with 401 if enabled but secret unconfigured | `None` | Yes when enabled — endpoint returns 401 if not configured | **High** — emergency operation secret | `EMERGENCY_KILLSWITCH_AUTH_SECRET=<emergency-bearer-token>` |

Auth behavior: expects `Authorization: Bearer <token>`. If enabled and `emergency_killswitch_auth_secret` is empty/`None`, all requests receive 401. Never log the token or header.

### Property

- `candle_timeframe_minutes` (`backend/app/core/config.py:77`) — parses `candle_timeframe` to int minutes (`"5 mins"` → `5`; `"15 mins"` → `15`; invalid → `5`).

### Not in `Settings` (ignored via `extra="ignore"`)

- `BROKER_MODE` / MockBroker switch — does not exist.
- `ALLOCATIONS_CONFIG_PATH` — routing uses Postgres `accounts`/`strategies`/`allocations`.
- Worker pool size, lease durations (`30s` lease, `15s` reclaim, `300s` claim staleness, `0.5s` idle poll) — hardcoded in `backend/app/main.py:102` and `backend/app/services/worker_pool.py:53`.
- Submit pacer `0.2s` — hardcoded in `backend/app/main.py:48` (`OrderSubmitPacer(min_interval_sec=0.2)`).
- Gateway pool / per-gateway or per-account host/port — not fields; `accounts.ibkr_account` is an IB account string, not a socket.

---

## 2. `DemoStreamSettings` — `backend/demo_streaming/config.py:7`

Isolated from trading execution config except for sharing `database_url` default. Loaded the same way (`env_file=".env"`, `extra="ignore"`, `case_sensitive=False`) via `get_demo_settings()` (`backend/demo_streaming/config.py:26`).

| Field | Env | Purpose | Where consumed | Default | Required | Sensitivity | Example |
|-------|-----|---------|----------------|---------|----------|-------------|---------|
| `database_url` | `DATABASE_URL` | Postgres URL for read-only PnL/position polling | `backend/demo_streaming/main.py:33` (`create_engine_from_settings()`) | `postgresql+asyncpg://root:root123@localhost:5433/ibkr_trading` | Yes in production | **High** | `DATABASE_URL=postgresql+asyncpg://user:<db-password>@db-host:5432/ibkr_trading` |
| `redis_url` | `REDIS_URL` | Redis URL for `positions:stream` | `backend/demo_streaming/main.py:36` (`Redis.from_url(settings.redis_url)`) | `redis://127.0.0.1:6379/0` | Yes if Redis not on localhost | Medium (may contain password) | `REDIS_URL=redis://127.0.0.1:6379/0` or `redis://:<redis-password>@redis-host:6379/0` |
| `demo_stream_host` | `DEMO_STREAM_HOST` | Host for demo SSE server | `backend/demo_streaming/main.py:69` (`uvicorn.Config(host=...)`) | `"127.0.0.1"` | No — set `0.0.0.0` for remote access | Low | `DEMO_STREAM_HOST=0.0.0.0` (remote) or `127.0.0.1` (local) |
| `demo_stream_port` | `DEMO_STREAM_PORT` | Port for demo SSE server | Same | `8010` | No | Low | `DEMO_STREAM_PORT=8010` |
| `demo_poll_interval_ms` | `DEMO_POLL_INTERVAL_MS` | DB poll interval for position/signal bridge | `backend/demo_streaming/main.py:56` (`PositionBridge(..., poll_interval=settings.demo_poll_interval_ms/1000)`) | `2000` | No | Low | `DEMO_POLL_INTERVAL_MS=2000` |
| `demo_signal_watch_limit` | `DEMO_SIGNAL_WATCH_LIMIT` | Max signals to watch per poll | Same | `500` | No | Low | `DEMO_SIGNAL_WATCH_LIMIT=500` |
| `demo_pnl_emit_interval_ms` | `DEMO_PNL_EMIT_INTERVAL_MS` | PnL emit interval | Same | `5000` | No | Low | `DEMO_PNL_EMIT_INTERVAL_MS=5000` |
| `demo_stream_maxlen` | `DEMO_STREAM_MAXLEN` | Redis stream maxlen (XADD MAXLEN) | `backend/demo_streaming/main.py:44` (`PositionStream(..., stream_maxlen=...)`) | `10000` | No | Low | `DEMO_STREAM_MAXLEN=10000` |
| `demo_stream_name` | `DEMO_STREAM_NAME` | Redis stream key | `backend/demo_streaming/main.py:43` / `60` | `"positions:stream"` | No | Low | `DEMO_STREAM_NAME=positions:stream` |
| `trading_api_url` | `TRADING_API_URL` | Trading API base URL that demo server proxies config CRUD to | `backend/demo_streaming/main.py:64` (`create_demo_app(..., trading_api_url=...)`) | `http://127.0.0.1:8000` | No | Low | `TRADING_API_URL=http://127.0.0.1:8000` |

Demo process logs to `storage/logs/demo-YYYY-MM-DD.log` (`backend/demo_streaming/main.py:32` `filename_prefix="demo"`); trading app logs to `storage/logs/trading-YYYY-MM-DD.log`.

---

## 3. Environment / `.env` notes

- Both settings classes use `env_file=".env"` with `extra="ignore"`. Extra keys in `.env` or environment are silently ignored.
- `case_sensitive=False` — `database_url`, `DATABASE_URL`, `Database_Url` all resolve the same field.
- Masked example `.env` (do not commit real secrets):

```env
# Secrets — use placeholders, load from vault in production
DATABASE_URL=postgresql+asyncpg://user:<db-password>@127.0.0.1:5433/ibkr_trading
WEBHOOK_AUTH_SECRET=<webhook-shared-secret>
EMERGENCY_KILLSWITCH_AUTH_SECRET=<emergency-bearer-token>
REDIS_URL=redis://127.0.0.1:6379/0

# App
APP_NAME=IBKR Paper Trading System
ENVIRONMENT=production
LOG_LEVEL=INFO

# IBKR (single socket — see docs/backend-multi-gateway.md)
IBKR_HOST=127.0.0.1
IBKR_PORT=7497
IBKR_CLIENT_ID=1
IBKR_CONNECTION_TIMEOUT=10

# Model Blue / paper
MODEL_BLUE_COMMITTED_NOTIONAL=
PAPER_EXECUTE_STK_AS_CFD=true

# Demo
DEMO_STREAM_HOST=127.0.0.1
DEMO_STREAM_PORT=8010
```

Use `get_settings()` / `get_demo_settings()`; never construct `Settings()` directly except in tests with `_env_file=None`.

---

## 17. Runtime / Deployment

> **Source file:** `docs/operations/runtime.md`  —  original heading: *Operations — Runtime*

**Verified from:** `backend/app/main.py:31`, `backend/app/db/session.py:16`, `backend/demo_streaming/main.py:30`, `backend/demo_streaming/config.py:7`, `backend/app/core/logger.py:18`, `docker-compose.yml`, `backend/scripts/*`, `backend/app/broker/ibkr/tws_client.py:470`.

> **Deployment-specific info** is marked with `[DEPLOYMENT]`. Localhost defaults below are development values; EC2/production overrides are noted per row.

---

## 1. Topology — two processes, one DB, one Redis, one TWS socket

```
                         ┌─────────────────────────────────┐
     TradingView  ──────▶ │  FastAPI main  :8000            │ ──▶  PostgreSQL :5433 (5432 in container)
     HTTP POST            │  backend/app/main.py:app         │      signal_jobs / signals / orders / baskets
     /api/webhooks/       │  uvicorn app.main:app            │      positions / execution_claims / kill_switch
     tradingview (202)    │  10-worker pool + reclaimer      │
                          │  RecoveryManager + Reconciler    │
                          └──────────┬──────────────────────┘
                                     │ placeOrder / callbacks
                                     ▼
                              TWS / IB Gateway (single socket)
                              host=IBKR_HOST port=IBKR_PORT
                              client_id=IBKR_CLIENT_ID
                                     │
                         ┌───────────┴───────────────────────┐
                         │  Demo stream :8010 (read-only)    │ ──▶  Redis :6379  stream positions:stream
                         │  python -m demo_streaming         │      PositionBridge polls Postgres every 2s
                         │  SSE + Vite dashboard proxy       │      No IBKR connection
                         └───────────────────────────────────┘
```

- **Main trading API** — FastAPI + async worker pool. Ingests webhooks into durable Postgres queue; executes via Model Blue → RMS → basket OMS → IBKR.
- **Demo stream** — isolated read-only poller/Redis publisher + SSE server (`backend/demo_streaming/main.py:30`). Does **not** connect to IBKR. Serves the React PnL dashboard.
- **One TWS/Gateway socket** — single `TWSClient` (`backend/app/main.py:41`), single `OrderSubmitPacer(0.2s)` (`backend/app/main.py:48`), all accounts multiplexed via `ib_order.account`. No multi-gateway pool or per-gateway limiter — see `docs/backend-multi-gateway.md`.

---

## 2. Processes and ports

| Process | Entrypoint | Default listen | Protocol | Auth |
|---------|------------|----------------|----------|------|
| Main API | `uvicorn app.main:app --host 127.0.0.1 --port 8000` (`backend/app/main.py:175`) | `127.0.0.1:8000` [DEPLOYMENT: bind host/port via reverse proxy or CLI flags] | HTTP — health, `POST /api/webhooks/tradingview` (202), `GET/POST /api/v1/config/*`, orders/reconcile/kill-switch | Webhook `X-Webhook-Secret`, emergency `Authorization: Bearer` (see `docs/operations/configuration.md`) |
| Demo stream | `python -m demo_streaming` (`backend/demo_streaming/__main__.py:1` → `backend/demo_streaming/main.py:99`) | `127.0.0.1:8010` (override via `DEMO_STREAM_HOST`/`DEMO_STREAM_PORT`) | HTTP SSE (`/demo/stream`), Redis `positions:stream` publisher, config CRUD proxy to `:8000` | None (read-only; do not expose `:8000` via ngrok) |
| PostgreSQL | `docker-compose.yml` `postgres:17` or managed Postgres [DEPLOYMENT] | `127.0.0.1:5433` → `5432` in container | `postgresql+asyncpg` | `DATABASE_URL` |
| Redis | Local `redis-server` or managed Redis [DEPLOYMENT] | `127.0.0.1:6379` | Redis protocol, stream `positions:stream` | `REDIS_URL` if passworded |
| TWS / Gateway | IBKR Trader Workstation or IB Gateway + IBC (see §3) [DEPLOYMENT] | `127.0.0.1:7497` paper TWS (default) | IBKR API socket (`EClient`) | `client_id` uniqueness |

### IBKR port map

| Port | Service | `paper_retries_allowed` (`backend/app/oms/retry_policy.py:7`) |
|------|---------|--------------------------------------------------------------|
| `7497` | **Paper TWS** — default `IBKR_PORT` (`backend/app/core/config.py:37`) | Yes |
| `4002` | **Paper Gateway** | Yes |
| `7496` | Live TWS | **No** — auto square-off retries disabled |
| `4001` | Live Gateway | **No** |

Live ports (`7496`/`4001`) are **not rejected** for ordinary trading; they only disable paper-only basket retry/square-off. See `backend/app/oms/retry_policy.py:10` and `docs/safety.md`.

### Localhost bindings

- Default `IBKR_HOST=127.0.0.1`, `DEMO_STREAM_HOST=127.0.0.1`, DB `localhost`. For remote dashboard access, set `DEMO_STREAM_HOST=0.0.0.0` [DEPLOYMENT] and open SG TCP `8010` (do **not** bind `app.main`/`ngrok` to expose the dashboard — use `:8010`). DB/Redis/TWS should remain localhost or VPC-private.

---

## 3. EC2 vs local

| Aspect | Local dev | EC2 / production [DEPLOYMENT] |
|--------|-----------|-------------------------------|
| Main API | `cd backend && .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000` | Same binary behind systemd supervisor or container; host `0.0.0.0` or reverse proxy; env injected via `/etc/environment` or secrets manager |
| Demo stream | `.venv/bin/python -m demo_streaming` → `127.0.0.1:8010` | `DEMO_STREAM_HOST=0.0.0.0` → `http://<PUBLIC_IP>:8010/`; SG allows TCP 8010; systemd unit or container |
| TWS/Gateway | Paper TWS desktop on `7497` | IB Gateway + **IBC** (`docs/infrastructure/` — IBC auto-login, headless Gateway) on `127.0.0.1:4002` or `:7497`; auto-restart on drop; daily 24h reset handled by IBC |
| Postgres | `docker compose up -d postgres` (`docker-compose.yml` → `5433:5432`) | Managed Postgres (RDS) or EC2 Docker volume `postgres_data`; `DATABASE_URL` points to private host; backups/snapshots outside compose |
| Redis | Local `redis-server` | `redis-server` on same host (`demo_streaming` only; main app does not use Redis) |
| Logs | `storage/logs/trading-YYYY-MM-DD.log` and `storage/logs/demo-YYYY-MM-DD.log` (`backend/app/core/logger.py:18` `LOG_DIR=.../storage/logs`) | Same path under app root (`/home/tradingapp/storage/logs` in docs) with daily midnight rollover (`backend/app/core/logger.py:39` `DatedTimedRotatingFileHandler(when="midnight")`); rotate/archiving via logrotate or CloudWatch |
| Env | `.env` file | Secrets manager / Parameter Store; `.env` not committed |

No `BROKER_MODE` — local and EC2 run the same IBKR code path against paper ports.

---

## 4. Lifespan order — `backend/app/main.py:31` `lifespan()`

Executed sequentially on startup; reversed on shutdown. Order matters (hydrate → connect → recovery → workers).

```
setup_logging(level=settings.log_level)                          backend/app/main.py:37
TWSClient()                                                      :41
IBKRExecutionAdapter(client, host, port, clientId, timeout,      :42  OrderSubmitPacer(0.2s)
  submit_pacer=OrderSubmitPacer(min_interval_sec=0.2))
OMSService(adapter)                                               :50
OrderManager(oms, symbol, quantity, ...)                          :53  DatabaseCommittedCapitalProvider, LivePnlService
  hydrate_runtime_from_db()                                       :65  processed_signals, open_positions, per_symbol_limits,
                                                                     default_symbol_limits, BasketCoordinator critical, kill_switch cache,
                                                                     execution retry policy
  client.connect_and_start(host,port,clientId,timeout)             :70  nextValidId handshake; warns if unconfirmed, does not crash
  hydrate_live_pnl()        [only if connect succeeded]            :80  resubscribe CFD market data for OPEN positions
  store on app.state: session_factory, client, ibkr_adapter,      :85  DI for route dependencies
                   oms, order_manager
  RecoveryManager.run_startup_recovery()                          :97  scan CLAIMED/PROCESSING jobs + EXECUTING/UNWINDING baskets,
                                                                     broker snapshot (best-effort), claim reconcile, requeue/quarantine
  ExecutionWorkerPool(worker_count=10).start()                   :102  10 mft-worker-* tasks + mft-stale-job-reclaimer
  PositionReconciler.start()                                     :110  30s interval broker-vs-ledger snapshot loop
  log startup summary: processed_signals, open_position_keys,    :118
                   critical_baskets, ibkr host:port
  ── yield (serve) ──
  position_reconciler.stop()                                     :138
  worker_pool.stop()                                             :140  cancel reclaimer + all workers, gather
  client.disconnect_clean()                                      :141  disconnect + join TWS thread (2s)
```

Hardcoded constants (not env):
- Worker pool: `worker_count=10`, `lease_duration_sec=30`, `reclaim_interval_sec=15`, `claim_stale_after_sec=300`, `idle_poll_interval_sec=0.5` (`backend/app/services/worker_pool.py:56`).
- Reconciler: `interval_sec=30`, `request_timeout_sec=15` (`backend/app/services/position_reconciler.py:27`).
- Pacer: `0.2s` (`backend/app/main.py:48`).
- Basket: `fill_timeout=90`, `cancel_timeout=30` (`backend/app/services/order_manager.py:163` → `backend/app/oms/coordinator.py:59`).

---

## 5. Dependencies

### PostgreSQL — `backend/app/db/session.py:16` `create_engine_from_settings()`

```python
create_async_engine(settings.database_url, pool_pre_ping=True,
                    pool_size=20, max_overflow=30, pool_timeout=30, pool_recycle=1800)
```

- Driver: `asyncpg` (`backend/pyproject.toml:9`). `DATABASE_URL` must be `postgresql+asyncpg://...`.
- `pool_pre_ping` probes stale connections; app tolerates transient DB hiccups with retry/log rather than crash (see `docs/operations/failure-recovery.md`).

### Redis — demo only

- `redis>=5.2.1` (`backend/pyproject.toml:17`). Main trading app **does not** use Redis. Demo uses `redis.asyncio.Redis.from_url(settings.redis_url)` (`backend/demo_streaming/main.py:36`) with `stream_name=positions:stream`, `maxlen=10000`, poll `2000ms`, PnL emit `5000ms`.

### IB Gateway / IBC [DEPLOYMENT]

- Paper: TWS `7497` or Gateway `4002` on `127.0.0.1`. IBC handles auto-login, auto-restart after the daily 24h Gateway reset, and reconnect. Without IBC, a manual Gateway restart disconnects `TWSClient` (`backend/app/broker/ibkr/tws_client.py:122` `connectionClosed` clears `next_order_id` and notifies listeners).
- Adapter detects drops via `is_connected()` (`backend/app/broker/ibkr/tws_client.py:465` checks `_connected_event` + `isConnected()`) and `fetch_broker_order_snapshot()` returns `False` when disconnected.

### System

- Python `>=3.12` (`backend/pyproject.toml:6`), `uvicorn>=0.52.1`, `fastapi>=0.141.1`, `ibapi>=9.81.1.post1`.
- Node `>=20` to build React dashboard (`frontend/` — Vite; proxied `/demo → :8010`).

---

## 6. Startup — commands and scripts

### Canonical commands

```bash
# Postgres (local)
docker compose up -d postgres          # docker-compose.yml: postgres:17 on 5433:5432

# Main trading API
cd backend
uv sync --extra dev
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
# or: .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000  [DEPLOYMENT]

# Demo stream (separate process)
.venv/bin/python -m demo_streaming
# remote: DEMO_STREAM_HOST=0.0.0.0 .venv/bin/python -m demo_streaming  # then http://<PUBLIC_IP>:8010/

# Frontend (optional separate Vite dev)
cd frontend && npm install && npm run dev   # http://127.0.0.1:5173/  (proxies /demo → :8010)
cd frontend && npm run build                # dist served by :8010 static (demo_streaming/static/*)
```

### `backend/scripts/*` (not startup orchestration — run manually)

| Script | Purpose | Needs |
|--------|---------|-------|
| `scripts/instrument_master/discover.py` | IBKR instrument discover CLI (contractDetails) | Running TWS + DB |
| `scripts/instrument_master/discover_cfd.py` | CFD discover | Same |
| `scripts/instrument_master/seed_fetcher.py` + `seed_paper_cfd.py` | Seed instruments from NASDAQ fetcher / paper CFD seed | DB |
| `scripts/oms/run_paper_execution.py` | Manual paper execution smoke | TWS + DB |
| `scripts/oms/flatten_gateway_positions.py` | Flatten all Gateway positions (operator tool) | TWS + DB |
| `scripts/test_tws_connection.py` / `test_tws_market_data.py` | TWS connectivity / market data smoke | TWS |
| `scripts/rms/run_demo.py` | RMS demo | DB |
| `scripts/load_test_mft_burst.py` | Burst N webhooks at live app (`--count`, `--audit`) | Live app + `DATABASE_URL` for audit |
| `scripts/prune_webhook_captures.py` | GC `data/tradingview_webhooks/` by age (`--days`, `--apply`) | Filesystem |
| `scripts/repair_historical_killswitch_positions.py` | One-time stale kill-switch position repair | DB |

No `docker/` directory or app `Dockerfile` exists in the checked-out tree — `docker-compose.yml` only defines `postgres`. [DEPLOYMENT] EC2 orchestration (systemd units, IBC service) lives outside this repo or under `docs/infrastructure/`.

---

## 7. Graceful shutdown — `backend/app/main.py:136` (post-`yield`)

```
position_reconciler.stop()   # cancel loop task
worker_pool.stop()           # running=False, cancel reclaimer, cancel 10 workers, gather
client.disconnect_clean()    # EClient.disconnect() + join TWS thread (2s) + clear registries
demo: _watch_shutdown → shutdown Event → cancel poll_task + close Redis + dispose engine  (backend/demo_streaming/main.py:86)
uvicorn timeout_graceful_shutdown=2s (demo) — in-flight SSE loops observe shutdown Event
```

SIGTERM/SIGINT triggers uvicorn lifespan teardown. In-flight jobs keep their `CLAIMED`/`PROCESSING` lease; on next boot `RecoveryManager` reconciles them (see `docs/operations/failure-recovery.md`). No `CORS`/`WebSocket`/static on `app.main` — those belong to `:8010`.

---

## 8. Logging

- Dir: `storage/logs` relative to repo root (`backend/app/core/logger.py:20` `LOG_DIR = ...parents[4]/storage/logs`). Production examples in docs use `/home/tradingapp/storage/logs`.
- Files: `trading-YYYY-MM-DD.log` (main), `demo-YYYY-MM-DD.log` (demo) — `backend/app/core/logger.py:27` `DatedTimedRotatingFileHandler` midnight rollover (`when="midnight"`, `backupCount=0`, no rename).
- Format: `%(asctime)s | %(levelname)-8s | %(name)s | %(trace)s | %(message)s` with trace `req= signal= trade= acct=` from `ContextVar`s (`backend/app/core/logger.py:18`).

---

## 18. Failure & Recovery

> **Source file:** `docs/operations/failure-recovery.md`  —  original heading: *Operations — Failure & Recovery*

**Verified from:** `backend/app/services/recovery.py:35`, `backend/app/services/worker_pool.py:50`, `backend/app/services/order_manager.py:581`, `backend/app/oms/coordinator.py:48`, `backend/app/broker/ibkr/tws_client.py:122`, `backend/app/oms/ibkr_adapter.py:255`, `backend/app/services/kill_switch.py:42`, `backend/app/services/position_reconciler.py:247`, `backend/app/db/models/signal.py:43`, `backend/app/api/routes/emergency.py:86`.

Only **implemented** behavior is documented. Aspirational multi-gateway / per-gateway limiter / reconnect-on-drop pool patterns are not implemented — see `docs/backend-multi-gateway.md`.

---

## 1. Crash / process failure

### 1a. Backend process fails (SIGKILL, OOM, unhandled exception)

| Aspect | Detail |
|--------|--------|
| **Failure** | FastAPI process dies mid-execution. |
| **Detection** | Process health check fails; systemd/container reports exit; or operator observes missing heartbeat / `health` endpoint down. No in-app crash detector — out-of-band monitoring required `[DEPLOYMENT]`. |
| **System behavior** | In-flight jobs remain in Postgres as `CLAIMED`/`PROCESSING` with a held `lease_expires_at` and possibly a held `execution_claims` row (`CLAIMED`). Baskets remain `EXECUTING`/`UNWINDING`. Broker orders may already be live at TWS. No duplicate submit on crash — leases/claims are the barrier. |
| **Recovery** | On next boot, `lifespan` runs `RecoveryManager.run_startup_recovery()` **before** the worker pool starts (`backend/app/main.py:95`). Steps (`backend/app/services/recovery.py:35`): 1) `SELECT signal_jobs WHERE status IN (CLAIMED, PROCESSING, RECOVERY_REQUIRED)` + `SELECT baskets WHERE state IN (EXECUTING, UNWINDING)`; 2) best-effort `fetch_broker_order_snapshot()` (fire-and-forget `reqOpenOrders`/`reqExecutions` — does not gate the decision); 3) `ExecutionClaimRepository.reconcile_stale_claims(stale_after_sec=0)` to release/seal claims held by the dead process; 4) per-job: if `count_orders_emitted>0` → `RECOVERY_REQUIRED` (quarantined, requires manual reconciliation); if `attempt_count>=max_attempts` → `DEAD_LETTER`; else → `QUEUED` (safe to retry, no orders emitted). Then `hydrate_runtime_from_db()` rebuilds `processed_signals`, `open_positions`, `symbol_exposures`, `_critical`, and kill-switch cache. |
| **Trading state** | Quarantined jobs **do not** auto-retry (prevents duplicate broker orders). Requeued jobs resume via worker pool. Accounts with `CRITICAL` baskets stay blocked for OPENs until operator clears. |

### 1b. Worker task crashes within a running process

| Aspect | Detail |
|--------|--------|
| **Failure** | A single `mft-worker-*` task raises an unhandled exception. |
| **Detection** | `worker_loop` catches `Exception`, logs, sleeps `0.5s`, continues (`backend/app/services/worker_pool.py:161`). Heartbeat task stops matching rows → lease expiry. |
| **System behavior** | Job lease expires (`lease_expires_at < NOW()`). Status remains `CLAIMED`/`PROCESSING` until reclaimed. Execution claim may stay `CLAIMED`. |
| **Recovery** | Two reclamation paths: 1) **Periodic reclaimer** `mft-stale-job-reclaimer` every `15s` (`backend/app/services/worker_pool.py:118`) reclaims stale jobs (`SignalJobRepository.reclaim_stale_jobs()` → requeue/quarantine/dead-letter) and reconciles stale claims (`ExecutionClaimRepository.reconcile_stale_claims(stale_after_sec=300)`); 2) Startup `RecoveryManager` also handles it after a full restart. |
| **Trading state** | Same as 1a per-job. No loss of durably persisted `signal_jobs` / `signals` inbox. |

---

## 2. Database fails (Postgres unavailable)

| Aspect | Detail |
|--------|--------|
| **Failure** | Postgres unreachable or `AsyncEngine` pool exhausted (`pool_size=20`, `max_overflow=30` — `backend/app/db/session.py:24`). |
| **Detection** | Webhook `create_job_if_not_exists` raises `Exception` → HTTP `500 "Failed to durably persist signal job"` (`backend/app/api/routes/webhooks.py:269`). Worker `claim_next_jobs` / `reclaim_stale_jobs` raise and are logged (`worker_pool.py:146`). `hydrate_runtime_from_db` logs `Failed to hydrate...` and continues with empty in-memory state (`backend/app/main.py:67`). TradingView retries on 5xx. |
| **System behavior** | Webhook does **not** return 202; TradingView should retry (HTTP 500). Already-queued jobs are not claimed until DB recovers. No broker submit occurs without a DB transaction (execution claims, `SignalRepository.record_inbound`, basket upsert all require a session). |
| **Recovery** | Automatic on DB restore: `pool_pre_ping=True` probes stale connections. Next webhook insert succeeds; workers claim next `QUEUED` job; leases resume heartbeating. No manual steps. If startup hydration failed, in-memory RMS state (`processed_signals`, `open_positions`) is incomplete until next `hydrate_runtime_from_db()` — restart the process after DB recovers to force a full hydrate. |
| **Trading state** | No new orders submitted while DB is down. Open position ledger (`positions`) is durable — not lost. |

---

## 3. Redis fails (demo only)

| Aspect | Detail |
|--------|--------|
| **Failure** | Redis unreachable. |
| **Detection** | Demo startup `await stream.ping()` raises and logs `Cannot reach Redis at ...` then re-raises (`backend/demo_streaming/main.py:51`), preventing demo from starting. Runtime: `PositionBridge` / `PositionStream` log and continue with backoff (demo helpers). |
| **System behavior** | **Main trading app unaffected** — it does not use Redis. Demo SSE stream emits no updates; dashboard shows stale data. Webhooks/execution continue. |
| **Recovery** | Restart `python -m demo_streaming` after Redis recovers. No reconciliation needed; `PositionBridge` re-polls Postgres (`poll_interval 2000ms`) and resumes publishing. |
| **Trading state** | Unaffected. |

---

## 4. Gateway / TWS disconnects

| Aspect | Detail |
|--------|--------|
| **Failure** | TWS/Gateway socket drops (network, Gateway 24h reset, duplicate `client_id`). |
| **Detection** | `TWSClient.connectionClosed()` fires (`backend/app/broker/ibkr/tws_client.py:122`): clears `_connected_event` and `next_order_id`, notifies listeners. `IBKRExecutionAdapter.on_connection_closed()` marks in-flight orders `ERROR "Connection closed unexpectedly"` (`backend/app/oms/ibkr_adapter.py:778`). `is_connected()` (`backend/app/broker/ibkr/tws_client.py:465`) returns `False`. |
| **System behavior** | New submits raise `ConnectionError("TWS connection unavailable")` (`backend/app/oms/ibkr_adapter.py:184`). Waiting baskets (`wait_for_terminal_or_fill`) continue to wait until `fill_timeout` (90s) then follow unwind/compensation/critical path. `fetch_broker_order_snapshot()` returns `False` when disconnected. IBC [DEPLOYMENT] auto-reconnects Gateway; otherwise operator must restart Gateway. |
| **Recovery** | Reconnect via `client.connect_and_start()` (called only at startup in `backend/app/main.py:70`). There is **no** automatic in-process reconnect loop — a restart of the main process triggers `hydrate_live_pnl()` and re-subscribes P&L if connected. Pending baskets that timed out during disconnect become `UNWINDING`/`CRITICAL` per `BasketCoordinator` logic and block new OPENs for that `(account_id, strategy_id)`. |
| **Trading state** | OPENs for affected `(account, strategy)` may be blocked (`_critical`). Existing orders already at broker remain live at IBKR — broker is source of truth. Reconcile via `PositionReconciler` (see §9). |

---

## 5. TWS socket drops mid-submit

| Aspect | Detail |
|--------|--------|
| **Failure** | `placeOrder()` throws or socket drops between `placeOrder` and broker ack. |
| **Detection** | Exception in `IBKRExecutionAdapter.submit_order` → marks order `ERROR` and re-raises (`backend/app/oms/ibkr_adapter.py:238`). `on_connection_closed` path above. |
| **System behavior** | Order stays `ERROR`. Basket path reaches `_resolve_failed_claim`: if `count_orders_emitted>0` the execution claim stays `CLAIMED` for reconciliation; if `0` the claim is released (`backend/app/services/order_manager.py:613`). The signal job is marked `FAILED` by the worker (`backend/app/services/worker_pool.py:349`). Incomplete basket → `UNWINDING` → cancel/compensation → `CRITICAL` if that fails. |
| **Recovery** | Execution claim reconciliation sweep (`300s`) or startup `reconcile_stale_claims` resolves orphaned claims against the ledger. Operator should audit `orders`/`baskets`/`signal_jobs` before retrying — `RECOVERY_REQUIRED` jobs are quarantined and not auto-retried. |
| **Trading state** | Claim remains held if any order was emitted — prevents silent duplicate submit. If no order emitted, job can be retried (worker will mark `FAILED`; next reclaim may requeue if under `max_attempts`). |

---

## 6. Order submission fails (validation / broker reject before `placeOrder`)

| Aspect | Detail |
|--------|--------|
| **Failure** | BMS/RMS rejects, instrument resolution fails, quantity `<=0`, or `submit_one_leg` raises before broker. |
| **Detection** | `OrderManager._evaluate_and_submit_locked` raises `ValueError` (`RMS check N rejected` `backend/app/services/order_manager.py:712` or `ZERO_QUANTITY` `:703`) which the worker maps to `REJECTED` (`backend/app/services/worker_pool.py:327`). `View100`-style contracts missing `resolved` raise `MISSING_RESOLVED_CONTRACT` (`backend/app/oms/ibkr_adapter.py:150`). |
| **System behavior** | No broker call; execution claim is released if nothing emitted (`_resolve_failed_claim` — `backend/app/services/order_manager.py:637`). Worker writes `REJECTED` with `RMS/OMS policy` error; `signals.status` becomes `REJECTED` (`backend/app/services/order_manager.py:385`). No exposure or open-position updates. |
| **Recovery** | No auto-retry for RMS rejects. Fix the signal/persisted config and re-alert with a new `signal_id`/`trade_id` (idempotency key is SHA256 of `strategy_id:signal_id:action` — `backend/app/services/worker_pool.py:27`). |
| **Trading state** | Clean — no orders, no positions, claim released. |

---

## 7. Order rejected by broker (after `placeOrder`)

| Aspect | Detail |
|--------|--------|
| **Failure** | TWS routes `on_error` with codes `200/201/10147/10148/10243` etc. or `orderStatus REJECTED/INACTIVE`. |
| **Detection** | `IBKRExecutionAdapter.on_error` maps `code in _ORDER_REJECTION_CODES` → `OMSOrderStatus.REJECTED` (`backend/app/oms/ibkr_adapter.py:742`); `on_order_status` maps `REJECTED`/`INACTIVE` → `REJECTED` (`backend/app/oms/ibkr_adapter.py:360`). Pending leg abort flag `abort_remaining=True` stops submitting remaining legs (`backend/app/oms/coordinator.py:252`). |
| **System behavior** | Basket: waits `fill_timeout`, then `_retry_incomplete` (paper only, within `retry_window_sec`, RMS-rechecked) or goes `UNWINDING` → cancel working → compensate filled legs. If compensation fails or rejected → `CRITICAL`. Worker maps `all_rejected` → `REJECTED`; incomplete `success==False` → `FAILED` (`backend/app/services/worker_pool.py:327`/`340`). Execution claim handling: if any order row exists, claim stays `CLAIMED` for reconciliation; else released. `REJECTED` baskets still record `UNWINDING`/`CRITICAL` exposure via `_record_unsettled_exposure` (`backend/app/services/order_manager.py:785`). |
| **Recovery** | Paper retry is gated: `paper_retries_allowed` only on `7497`/`4002`; RMS re-evaluation per retry leg; deduped by `retry_key` (`backend/app/oms/coordinator.py:619`). Live ports do not retry. `CRITICAL` blocks new OPENs for that `(account, strategy)` until operator investigates and clears (restart hydrates but `CRITICAL` persists in DB — see `docs/backend-execution.md`). |
| **Trading state** | Partial fills book unsettled exposure (`backend/app/services/order_manager.py:879` `_record_unsettled_exposure`) and may mark basket `CRITICAL` → OPEN gate closed. No duplicate retry while claim held. |

---

## 8. Partial fill

| Aspect | Detail |
|--------|--------|
| **Failure** | Some legs of a basket fill, others do not (by `fill_timeout=90s` or broker partial). |
| **Detection** | `BasketCoordinator._basket_complete` checks filled vs intended per leg within `1e-8` (`backend/app/oms/coordinator.py:462`). `on_exec_details` updates `filled_quantity`/`executions` and may set `PARTIALLY_FILLED` (`backend/app/oms/ibkr_adapter.py:609`). |
| **System behavior** | Same path as §7: `_retry_incomplete` (paper) for the remaining qty (cancel-working → sleep `retry_interval_sec` → RMS-recheck → submit single-leg retry). If retries exhausted or `retry_window_sec` expired → `UNWINDING` → cancel → `compensate_filled` (reverse legs). If `fill_timeout` is the `retry_window` cap, retries are bounded. `BasketState.COMPENSATED` on success; `CRITICAL` on failure. `OMSOrder.last_exec_id` and `executions_weighted_average` preserve fill precision (`backend/app/oms/ibkr_adapter.py:442`). |
| **Recovery** | Auto-retries are compensating, not blind re-submits — each retry is RMS-checked and cancel-confirmed. `CRITICAL` is terminal and requires operator reconciliation (audit `orders.executions`, broker statement, `positions`). |
| **Trading state** | Filled qty is persisted per `execDetails`; unsettled exposure is booked (`_record_unsettled_exposure`). OPEN not marked unless basket reaches `OPEN`/`CLOSED`. |

---

## 9. Position mismatch (broker vs ledger)

| Aspect | Detail |
|--------|--------|
| **Failure** | IBKR position qty ≠ ledger `positions` net qty per `(account_id, symbol, sec_type)`. |
| **Detection** | `PositionReconciler` every `30s` (`backend/app/services/position_reconciler.py:247`): `request_positions_async(timeout=15)` → `BrokerPositionRepository.replace_snapshot()` → `build_ledger_net_lines` vs `classify_reconcile_diffs` → `insert_run` + `EventRepository "POSITION_RECONCILE"`. Classification (`backend/app/services/position_reconciler.py:34`): `MATCH`, `LEDGER_GHOST` (ledger without broker), `BROKER_ORPHAN` (broker without ledger), `QTY_DRIFT`, `UNMAPPED_ACCOUNT`; each carries `in_flight` flag for accounts with `EXECUTING`/`UNWINDING` baskets or `PROCESSING` jobs. Skips `LEDGER_GHOST` when `timed_out==True`; skips entire sweep when TWS disconnected. |
| **System behavior** | **Read-only** — logs `warning` when mismatches/timed_out/error, `info` when all matched; persists `broker_position_snapshots` + `reconcile_runs`. Does **not** flatten or amend positions. In-flight accounts are flagged so `QTY_DRIFT` during active execution is expected. |
| **Recovery** | Operator audits `reconcile_runs.mismatches` and broker `executions`/`openOrders`. Flatten via `POST /api/v1/reconcile/positions/flatten` (`backend/app/api/routes/config.py` reconcile wiring) or manual IBKR. Kill-switch flatten uses a separate path (§10). |
| **Trading state** | Ledger remains authoritative for RMS `open_positions`/`symbol_exposures` until corrected. Reconciler `QTY_EPSILON=1e-6`. |

---

## 10. Kill switch activates (operator `POST /api/v1/kill-switch/activate`)

| Aspect | Detail |
|--------|--------|
| **Failure** | Operator-initiated emergency flatten (trusted channel). |
| **Detection** | `POST /api/v1/kill-switch/activate {account_id}` (`backend/app/api/routes/config.py` + `backend/app/services/kill_switch.py:156` `initiate_square_off`). Idempotent: active `ACTIVATING/FLATTENING/RECONCILING/RETRYING` returns existing `operation_id`. |
| **System behavior** | Creates `kill_switch_operations` row `ACTIVATING` with `initial_position_count`; arms in-memory cache `_KILL_SWITCH_ACTIVE_ACCOUNTS` **and** DB (`_ARMED_STATUSES` includes `COMPLETE`/`UNRESOLVED` — switch stays armed after flatten completes). `execute_flatten_operation_background` spawns bounded `asyncio.gather` (semaphore `5`) of `EMERGENCY_FLATTEN` CLOSE intents via `BasketCoordinator` (`backend/app/services/kill_switch.py:338`). RMS is bypassed with synthetic `PASS` (`KILL_SWITCH_EMERGENCY_CLOSE`). Each position close persists `positions.risk_state` → closed and `event_log POSITION_CLOSE KILL_SWITCH`. Reconciles via DB `FILLED` kill-switch orders (req from `orders`) — if `len(filled_close) >= req_legs` the ledger position is closed even if background tasks missed it. Final status `COMPLETE` (all flat) or `UNRESOLVED` (remnants). Events `KILL_SWITCH_ACTIVATED/COMPLETED/UNRESOLVED` emitted. |
| **Recovery** | OPENs for that `account_id` are blocked (`OrderManager._fanout_single_account` checks `is_account_kill_switch_active` → `ValueError KILL_SWITCH_ACTIVE` `backend/app/services/order_manager.py:453`; `hydrate_kill_switch_cache` re-arms after restart `backend/app/services/kill_switch.py:75`). Operator must `POST /api/v1/kill-switch/clear {account_id}` (`backend/app/services/kill_switch.py:105` `clear_account_kill_switch` → sets `CLEARED`) before new OPENs are allowed. |
| **Trading state** | All OPEN positions for the account are flattened (or marked `UNRESOLVED`). Account remains **armed** until explicit clear — restart does not disarm (rehydrated from DB). |

---

## 11. Emergency kill switch (external `POST /emergency-kill-switch`)

| Aspect | Detail |
|--------|--------|
| **Failure** | External emergency webhook arms the halt without broker orders. |
| **Detection** | `POST /emergency-kill-switch {ibkr_account_id}` (`backend/app/api/routes/emergency.py:86`) with `Authorization: Bearer <EMERGENCY_KILLSWITCH_AUTH_SECRET>` via `hmac.compare_digest` (`backend/app/api/routes/emergency.py:66`). Fails closed 401 if enabled but secret unconfigured/missing/malformed. Lowercases `ibkr_account` lookup via `func.upper`. |
| **System behavior** | `KillSwitchService.arm_account_kill_switch_only` (`backend/app/services/kill_switch.py:229`) — same DB+cached arm as §10 but **does not** submit broker flatten orders. Returns existing operation if already armed (`_ARMED_STATUSES`). Log `EMERGENCY KILL SWITCH ARMED (NO BROKER FLATTEN)`. |
| **Recovery** | Same armed semantics: OPENs blocked, rehydrated on restart, requires `POST .../clear` to disarm. Broker positions remain live and must be flattened manually or via the full kill-switch activate path. |
| **Trading state** | No orders sent; ledger untouched; halt is logical only. |

---

## 12. Cross-cutting guarantees

| Property | Mechanism | File |
|----------|-----------|------|
| Exactly-once enqueue per TradingView alert | SHA256 `strategy_id:signal_id:action` idempotency key; `create_job_if_not_exists` conflict-do-nothing | `backend/app/services/worker_pool.py:27`, `backend/app/api/routes/webhooks.py:250` |
| Execution dedupe across crashes/workers | `execution_claims` barrier acquired **after** RMS+instrument resolve, **before** broker submit (`backend/app/services/order_manager.py:744`), committed in its own transaction; `_seal` on `OPEN/CLOSED`, `_resolve_failed_claim` checks `count_orders_emitted` | `backend/app/services/order_manager.py:581`, `backend/app/db/repositories/execution_claim_repository.py:1` |
| Job lease prevents concurrent duplicates | `claim_next_jobs(lease_duration_sec=30)`, `heartbeat_lease` every `lease_duration/3`, `fence=True` on status writes; `domain_lock (account_scope, strategy_id)` serializes same-strategy work | `backend/app/services/worker_pool.py:77`, `backend/app/db/repositories/signal_repository.py` |
| Basket corruption prevention | `BasketCoordinator._retry_ids`/`_critical`/`_order_baskets`; per-basket idempotency keys on events; no `_active_basket` shared mutable (fixed) | `backend/app/oms/coordinator.py:74`, `docs/production_mft_baseline.md:82` historical |
| Pacer overflow protection | Single `OrderSubmitPacer(0.2s)` for all accounts; early-warning `>5` waiters | `backend/app/main.py:48`, `backend/app/oms/submit_pacer.py` |

---

# PART VIII — TESTING


---

## 19. Testing

> **Source file:** `docs/testing/testing.md`  —  original heading: *Testing*

**Verified from:** `backend/pyproject.toml`, `backend/tests/conftest.py:1`, `backend/tests/ibkr_test_utils.py:1`, `backend/tests/**/test_*.py`, `backend/app/main.py:31`, `backend/docs/DEVELOPER_EXECUTION_GUIDE.md` commands.

---

## 1. How to run

From `backend/` (package name `backend`, Python `>=3.12` — `backend/pyproject.toml:6`):

```bash
cd backend

# Install deps (dev extras: httpx, mypy, pytest, pytest-asyncio, ruff)
uv sync --extra dev          # or use existing .venv

# All tests
.venv/bin/pytest

# With verbose / single module
.venv/bin/pytest -v
.venv/bin/pytest tests/test_oms.py tests/test_basket_coordinator.py

# Lint (ruff)
.venv/bin/ruff check app/ tests/ scripts/
```

Pytest config — `backend/pyproject.toml:41`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

`[project.optional-dependencies] dev` = `httpx`, `mypy`, `pytest>=9.1.1`, `pytest-asyncio>=1.4.0`, `ruff>=0.16.2`.

Environment: `DATABASE_URL` defaults to `postgresql+asyncpg://root:root123@localhost:5433/ibkr_trading` (`backend/app/core/config.py:31`). Tests that touch Postgres need a reachable DB; many suites mock IBKR and need no live TWS. `PAPER_EXECUTE_STK_AS_CFD` is forced `false` in tests (`backend/tests/conftest.py:8` `os.environ.setdefault("PAPER_EXECUTE_STK_AS_CFD","false")`), overriding the production default `true`.

---

## 2. Fixtures — `backend/tests/conftest.py:1`

| Fixture | Scope | What it does | Where used |
|---------|-------|--------------|------------|
| `session_factory` (`conftest.py:14`) | function (async) | Creates an `async_sessionmaker` bound to `create_async_engine(settings.database_url)`; yields then `engine.dispose()`. Gives tests a fresh DB session factory without touching global `AsyncSessionLocal`. | Any DB integration test (`test_basket_coordinator`, `test_kill_switch`, `test_reconcile_api`, etc.) |
| `_redirect_webhook_capture_dir` (`conftest.py:23`, `autouse=True`) | function | Monkeypatches `app.api.routes.webhooks.WEBHOOK_CAPTURE_DIR` to `tmp_path/tradingview_webhooks` so CSV/JSON captures in `backend/tests/test_tradingview_webhook.py` do not pollute `data/` on host. | All tests (autouse) |

Additional per-file fixtures (e.g. `TestClient` with patched `TWSClient.connect_and_start`, `ExecutionWorkerPool.start/stop`, `PositionReconciler.start/stop` in `backend/tests/test_tradingview_webhook.py:27`) are file-local.

---

## 3. Mocks — `backend/tests/ibkr_test_utils.py:1`

**Purpose:** Mock IBKR fills for `OrderManager`/basket tests. Never used against a real Gateway. Header comment (`ibkr_test_utils.py:1`): `Mock IBKR fills for OrderManager/basket tests. Never used against a real Gateway.`

| Helper | Signature | Behavior |
|--------|-----------|----------|
| `_fill_px(order)` | `(order: Any) -> float` | Reads `order.lmtPrice`; if finite `0 < px < 1e12` returns it, else `10.0`. |
| `fill_on_place_order(adapter, client)` | `(adapter, client) -> None` | Installs `client.placeOrder.side_effect = fake_place_order` that immediately calls `adapter.on_order_status(orderId, "Filled", qty, 0.0, px, ...)` so `BasketCoordinator`/`OrderManager` tests observe a full fill without a broker. Uses `adapter._orders_by_tws_id` and `oms.limit_price` when available. |

Usage pattern (from e.g. `backend/tests/test_order_manager.py`, `backend/tests/test_basket_coordinator.py`):

```python
from tests.ibkr_test_utils import fill_on_place_order
fill_on_place_order(adapter, client)  # client is a MagicMock TWSClient
```

Broader mocking style: `unittest.mock.MagicMock`/`AsyncMock` for `TWSClient`, `IBKRExecutionAdapter`, `BasketCoordinator`, direct `patch("app.broker.ibkr.tws_client.TWSClient.connect_and_start")` in lifespan tests (`backend/tests/test_api.py`).

---

## 4. Test files — suites and intent

50 Python modules under `backend/tests/` (48 `test_*.py` + `conftest.py` + `ibkr_test_utils.py`). Count includes `rms/` subpackage. Header counts use `backend/docs/backend-testing.md:21` inventory cross-checked with on-disk list.

### Top-level `backend/tests/test_*.py` (42 files)

| File | Suite / Intent |
|------|----------------|
| `test_api.py` | Health, webhooks (202 enqueue), orders, lifespan wiring |
| `test_app_wiring.py` | DI / component wiring (main `lifespan` wiring checks) |
| `test_basket_coordinator.py` | `BasketCoordinator` atomicity — submit/wait/unwind/compensation/CRITICAL (mocked IBKR) |
| `test_basket_retry.py` | Paper retry policy + `OrderSubmitPacer` integration |
| `test_broker_flatten_api.py` | `POST /api/v1/reconcile/positions/flatten` |
| `test_burst_stress_150_300.py` | Burst stress 150/300 webhooks (heavy, mocked) |
| `test_burst_stress_500_and_kill_switch.py` | Burst stress + kill switch under load (heavy) |
| `test_cfd_discover.py` | CFD discover (`ensure_cfd_instruments_for_symbols`) mocked |
| `test_close_single_pair.py` | CLOSE single pair (one-leg close path) |
| `test_config.py` | `Settings` defaults / env-parsing / validation |
| `test_config_api.py` | Config CRUD HTTP (`/api/v1/config/*`) + RMS limit reload |
| `test_config_service.py` | `AccountStrategyConfigService` validation |
| `test_database.py` | SQLAlchemy / DB connection / session |
| `test_db_model_blue_persistence.py` | Model Blue trades persistence across sessions |
| `test_default_symbol_limit.py` | Default symbol limit per account (RMS) |
| `test_demo_streaming.py` | Demo streaming helpers (`PositionBridge`, `PositionStream`) — no IBKR |
| `test_demo_streaming_signal_persistence.py` | Demo signal display / persistence read-only path |
| `test_emergency_kill_switch.py` | `POST /emergency-kill-switch` auth + arm-only (no broker orders) |
| `test_execution_audit_persistence.py` | Execution ledger / fill precision (`executions`, commission) |
| `test_hardening_lifecycle.py` | Quantity gate, RMS audit, persist, PnL, CLOSE — production hardening |
| `test_instrument_master_discover.py` | `scripts/instrument_master/discover.py` (mocked IBKR) |
| `test_instrument_resolution.py` | Instrument / contract resolution (`attach_resolved`, `ibkr_contract_from_resolved`) |
| `test_kill_switch.py` | Kill switch service + `EMERGENCY_FLATTEN` RMS bypass |
| `test_kill_switch_reconciliation_fix.py` | Kill switch ledger reconciliation (stale positions → CLOSED) |
| `test_kill_switch_start_again.py` | Kill switch re-arm / clear / idempotency |
| `test_logger.py` | Logging infrastructure (`setup_logging`, `TraceContextFilter`, `DatedTimedRotatingFileHandler`) |
| `test_market_data_pipeline.py` | Live PnL / market data pipeline (`LivePnlService`) |
| `test_mft_concurrency_recovery.py` | Worker pool + claims + recovery + `IBKRExecutionScheduler` (scheduler tests-only) |
| `test_model_blue.py` | Model Blue parser and sizer |
| `test_models.py` | Domain models (`OrderIntent`, `RMSContext`, etc.) |
| `test_multi_account_routing.py` | Strategy → eligible accounts fan-out (`DatabaseStrategyAccountRouter`) |
| `test_naked_pair_protection_fix.py` | Naked pair protection (single-leg OPEN reject) |
| `test_n_leg_execution.py` | N-leg RMS/OMS vs Model Blue isolation |
| `test_oms.py` | OMS + `IBKRExecutionAdapter` offline (order build, status mapping, error codes) |
| `test_order_manager.py` | `OrderManager` facade — strategy routing, RMS, OMS, claims |
| `test_pacer.py` | `scripts.instrument_master.pacer.RatePacer` (discover CLI) — **not** `OrderSubmitPacer` |
| `test_persistent_schema.py` | Persistent schema unit tests (constraints, defaults) |
| `test_position_reconciler.py` | `PositionReconciler` snapshot + ledger diff classification (no live TWS) |
| `test_production_path_hardening.py` | Production-path gaps (mocked IBKR) |
| `test_reconcile_api.py` | `GET /api/v1/reconcile/positions` |
| `test_repair_historical_killswitch_positions.py` | `scripts/repair_historical_killswitch_positions.py` |
| `test_seed_fetcher.py` | NASDAQ seed fetcher fixtures (`scripts/instrument_master/seed_fetcher.py`) |
| `test_signal_payload_persistence.py` | Webhook JSON / pair / side audit (`SignalRepository`) |
| `test_stk_to_cfd_demo_override.py` | Paper `STK→CFD` execution override (`paper_execute_stk_as_cfd`) |
| `test_system_monitor.py` | `SystemMonitorService` / system status API |
| `test_tradingview_execution_integration.py` | Webhook → sizer → RMS → OMS → `IBKRExecutionAdapter` integration (mocked IBKR) |
| `test_tradingview_signal_persistence.py` | Signal persistence (`signals` inbox) |
| `test_tradingview_webhook.py` | TradingView webhook API (HTTP 202 enqueue, idempotency, CSV captures) |
| `test_tws_connection.py` | `TWSClient` lifecycle (`connect_and_start`, `connectionClosed`, `nextValidId`) |
| `test_webhook_authentication.py` | Webhook `X-Webhook-Secret` auth (401/202) |

### `backend/tests/rms/` (6 files)

| File | Intent |
|------|--------|
| `rms/test_rms_engine.py` | `RMSEngine` orchestration (check ordering, PASS/REJECT) |
| `rms/test_rms_duplicate.py` | Check 2 — duplicate signal (`(account, strategy, trade_id)`) |
| `rms/test_rms_strategy.py` | Check 3 — strategy gate |
| `rms/test_rms_contract_month.py` | Check 4 — contract month |
| `rms/test_rms_position_limit.py` | Check 7 — position limit (`max_open_positions`) |
| `rms/test_rms_money_per_stock.py` | Check 8 — money-per-stock / exposure & open-position gate |

---

## 5. Major suites — suggested runs by area

Commands use `backend/` as cwd. Adapted from `backend/docs/backend-testing.md:76` and file inventory above.

| Area | Command |
|------|---------|
| All (fast) | `.venv/bin/pytest -q --ignore=tests/test_burst_stress_150_300.py --ignore=tests/test_burst_stress_500_and_kill_switch.py` |
| Webhook / enqueue | `.venv/bin/pytest tests/test_tradingview_webhook.py tests/test_webhook_authentication.py tests/test_api.py` |
| RMS | `.venv/bin/pytest tests/rms/` |
| OMS / basket | `.venv/bin/pytest tests/test_oms.py tests/test_basket_coordinator.py tests/test_basket_retry.py tests/test_n_leg_execution.py` |
| OrderManager / Model Blue | `.venv/bin/pytest tests/test_order_manager.py tests/test_model_blue.py tests/test_n_leg_execution.py` |
| Concurrency / claims / recovery | `.venv/bin/pytest tests/test_mft_concurrency_recovery.py tests/test_tradingview_webhook.py tests/test_hardening_lifecycle.py` |
| Kill switch | `.venv/bin/pytest tests/test_kill_switch.py tests/test_kill_switch_reconciliation_fix.py tests/test_kill_switch_start_again.py tests/test_emergency_kill_switch.py` |
| Position reconcile | `.venv/bin/pytest tests/test_position_reconciler.py tests/test_reconcile_api.py tests/test_broker_flatten_api.py` |
| Full integration (mocked IBKR) | `.venv/bin/pytest tests/test_tradingview_execution_integration.py tests/test_hardening_lifecycle.py tests/test_production_path_hardening.py` |
| Config | `.venv/bin/pytest tests/test_config.py tests/test_config_api.py tests/test_config_service.py` |
| Stress (heavy / slow) | `.venv/bin/pytest tests/test_burst_stress_150_300.py tests/test_burst_stress_500_and_kill_switch.py` |

Many tests use mocked IBKR and do not require a live TWS connection. Tests touching Postgres need `DATABASE_URL`. Burst stress tests are slow and are best run in isolation.

---

## 6. Operational scripts (not pytest — need a running app)

| Script | Role | Command |
|--------|------|---------|
| `scripts/load_test_mft_burst.py` | Burst N webhooks at a live app; reports ack rate, latency percentiles; with `--audit` checks `signal_jobs` statuses via `DATABASE_URL` | `.venv/bin/python scripts/load_test_mft_burst.py --count 150 --audit` |
| `scripts/prune_webhook_captures.py` | GC raw captures under `data/tradingview_webhooks/` by age (`--days`, `--apply`; dry run unless `--apply`) | `.venv/bin/python scripts/prune_webhook_captures.py --days 14 --apply` |
| `scripts/repair_historical_killswitch_positions.py` | One-time repair of stale kill-switch positions | `.venv/bin/python scripts/repair_historical_killswitch_positions.py` |

---

## 7. Notes

- Inventory is of test **modules**, not line coverage.
- `test_pacer.py` covers the discover CLI `RatePacer` (`backend/scripts/instrument_master/pacer.py`), not the production `OrderSubmitPacer` (`backend/app/oms/submit_pacer.py`) — the latter is covered via `test_basket_retry.py`/`test_oms.py`.
- `IBKRExecutionScheduler` in `backend/broker/ibkr/scheduler.py` is tests-only; production submit pacing is `OrderSubmitPacer(0.2s)` (`backend/app/main.py:48`). See `docs/safety.md`.
- This file is an index; per-suite checklists and gaps live in `docs/backend-testing.md`.

---

# PART IX — DIAGRAMS


---

## 20. Diagram — Overall System

> **Source file:** `docs/diagrams/overall-system.md`  —  original heading: *Diagram: Overall System*

```mermaid
flowchart LR
    TV["TradingView"] --> API["API :8000"]
    API --> DB[("Postgres")]
    DB --> DEMO["Demo :8010"]
    DEMO --> FE["Frontend"]
    API --> GW["Gateway :7497"]
    GW --> IBKR["IBKR"]
```

---

## 21. Diagram — Normal Trading Signal

> **Source file:** `docs/diagrams/trading-signal.md`  —  original heading: *Diagram: Normal Trading Signal*

```mermaid
sequenceDiagram
    participant TV as TradingView
    participant API as API :8000
    participant DB as Postgres
    participant ENG as Engine
    participant GW as Gateway

    TV->>API: POST /webhooks/tradingview
    API->>DB: job (idempotent)
    API-->>TV: 202
    DB->>ENG: workers → RMS → Basket
    ENG->>GW: placeOrder (0.2s)
    GW-->>ENG: Filled
    ENG->>DB: persist
```

---

## 22. Diagram — Order Execution (Basket)

> **Source file:** `docs/diagrams/order-execution.md`  —  original heading: *Diagram: Order Execution (Basket)*

```mermaid
sequenceDiagram
    participant OM as OrderManager
    participant BC as Basket
    participant GW as Gateway

    OM->>BC: execute (N legs)
    BC->>GW: submit paced 0.2s
    GW-->>BC: fills
    BC->>BC: all filled? → OPEN/CLOSED
    BC-->>OM: or COMPENSATED / CRITICAL
```

---

## 23. Diagram — IBKR Connection Lifecycle

> **Source file:** `docs/diagrams/ibkr-connection.md`  —  original heading: *Diagram: IBKR Connection Lifecycle*

```mermaid
sequenceDiagram
    participant APP as App
    participant TWS as TWSClient
    participant GW as Gateway :7497

    APP->>TWS: connect_and_start
    TWS->>GW: connect + nextValidId
    GW-->>TWS: handshake OK

    loop trading
        TWS<->>GW: placeOrder / callbacks
    end

    APP->>TWS: disconnect_clean
```

---

## 24. Diagram — Position Reconciliation

> **Source file:** `docs/diagrams/position-reconciliation-diagram.md`  —  original heading: *Diagram: Position Reconciliation*

```mermaid
sequenceDiagram
    participant RC as Reconciler 30s
    participant GW as Gateway
    participant DB as Postgres

    loop every 30s
        RC->>GW: reqPositions
        GW-->>RC: snapshot
        RC->>DB: diff + run
    end
```

---

## 25. Diagram — Kill Switch

> **Source file:** `docs/diagrams/kill-switch.md`  —  original heading: *Diagram: Kill Switch*

```mermaid
sequenceDiagram
    participant OP as Operator
    participant API as API :8000
    participant KS as KillSwitch
    participant GW as Gateway

    OP->>API: POST square-off
    API->>KS: arm + flatten (sem 5)
    KS->>GW: MKT closes (0.2s)
    GW-->>KS: Filled
    KS-->>OP: COMPLETE (armed)

    OP->>API: POST .../clear
    API-->>OP: CLEARED — OPENs resume
```

---

## 26. Diagram — Emergency Kill Switch (Local vs EC2)

> **Source file:** `docs/diagrams/emergency-kill-switch.md`  —  original heading: *Diagram: Emergency Kill Switch (Local vs EC2)*

```mermaid
sequenceDiagram
    participant OP as Operator
    participant EC2 as EC2 :8000
    participant LOC as Local Script
    participant GW as Gateway

    OP->>EC2: POST /emergency-kill-switch (Bearer)
    EC2-->>OP: 200 armed (no orders)

    OP->>LOC: flatten --account X --apply
    LOC->>GW: reqPositions → MKT closes (0.2s)
    GW-->>LOC: Filled
```

---

## 27. Diagram — Gateway Failure / Reconnect

> **Source file:** `docs/diagrams/gateway-failure.md`  —  original heading: *Diagram: Gateway Failure / Reconnect*

```mermaid
sequenceDiagram
    participant GW as Gateway
    participant APP as App
    participant DB as Postgres

    GW-->>APP: connectionClosed
    APP->>APP: mark orders ERROR
    APP->>DB: baskets → CRITICAL
    APP->>GW: restart → connect_and_start
```

---

## 28. Diagram — Frontend → Backend → Streaming

> **Source file:** `docs/diagrams/frontend-streaming.md`  —  original heading: *Diagram: Frontend → Backend → Streaming*

```mermaid
sequenceDiagram
    participant FE as Frontend
    participant DEMO as Demo :8010
    participant DB as Postgres

    FE->>DEMO: GET /demo/positions
    DEMO->>DB: snapshot
    DEMO-->>FE: legs

    loop every 2s
        DEMO->>DB: poll → diff
        DEMO-->>FE: SSE event
    end
```

---

## 29. Diagram — Account Configuration Flow

> **Source file:** `docs/diagrams/account-config-flow.md`  —  original heading: *Diagram: Account Configuration Flow*

```mermaid
sequenceDiagram
    participant FE as Frontend
    participant API as API :8000
    participant DB as Postgres

    FE->>API: create account / allocation
    API->>DB: INSERT + checks
    FE->>API: set limits / execution settings
    API->>DB: upsert + reload
```

---

# PART X — AUDIT


---

## 30. Documentation Audit Report

> **Source file:** `docs/DOCUMENTATION_AUDIT.md`  —  original heading: *Documentation Audit Report*

> Generated after a complete read-only codebase audit and second-pass verification. Every claim was checked against the actual repository — filenames, imports, class definitions, route decorators, call sites, DB models, and migrations.

## Repository Inspected

### Directories

| Path | What was inspected |
|---|---|
| `backend/app/main.py` | Lifespan, wiring, startup order |
| `backend/app/core/config.py` | Settings (all fields) |
| `backend/app/core/logger.py`, `identifiers.py` | Logging, strategy_id normalization |
| `backend/app/api/router.py`, `routes/*` | 26 route decorators across 7 route files |
| `backend/app/db/models/*` (13 files, 17 models) | Every table, column, PK/FK/index |
| `backend/app/db/repositories/*` (11 repos) | Every repository method signature |
| `backend/app/db/base.py`, `session.py` | Base, engine, pool settings |
| `backend/alembic/*` + 18 version files | Full migration chain, head `f4a8c2d1e903` |
| `backend/app/services/*` | OMS pipeline, workers, recovery, kill switch, reconciler, PnL, model blue, strategies |
| `backend/app/oms/*` | `oms_service.py:17`, `coordinator.py:48`, `basket.py:23`, `models.py:12`, `submit_pacer.py:12`, `retry_policy.py:6` |
| `backend/app/rms/*` + `checks/*` (5 checks) | `engine.py:37`, checks 2/3/4/7/8 |
| `backend/app/broker/ibkr/*` | `tws_client.py:16`, `ibkr_adapter.py:43`, `scheduler.py` (tests-only), `positions.py` |
| `backend/app/instruments/*` | `resolver.py`, `cfd_discover.py`, `models.py`, `execution_override.py`, `paper_cfd_catalog.py` |
| `backend/app/accounts/*` | `router.py`, `config_service.py`, `context.py` |
| `backend/app/schemas/*` | 5 schema files |
| `backend/demo_streaming/*` (7 files) | `main.py`, `api.py`, `publisher.py`, `snapshot.py`, `stream.py`, `config.py`, `__main__.py` |
| `backend/scripts/*` | `flatten_gateway_positions.py`, instrument_master, load_test, etc. |
| `backend/tests/*` (50 test modules) | Filenames + brief purposes |
| `backend/pyproject.toml` | Dependencies, requires-python 3.12, tool config |
| `backend/.env`, `.env.example`, `alembic.ini` | Env handling |
| `frontend/src/*` | `App.tsx`, `pages/*` (6 pages), `components/*` (14), `store/*`, `hooks/*`, `api/*`, `types/*`, `utils/*` |
| `frontend/package.json`, `vite.config.ts`, `tsconfig.*` | Frontend stack, proxy config |
| `docker-compose.yml`, `docker/`, `Readme.md`, `AGENTS.md`, `docs/*` (pre-existing) | Infrastructure + existing docs |
| `ibkr-tws-price-streaming-guide.html`, `Execution_System_Architecture.md` (parent) | Additional refs |

### Major Modules

* **Signal ingestion**: `webhooks.py` → `SignalJobRepository` → `ExecutionWorkerPool`
* **Strategy layer**: `ModelBlueStrategy`, `StrategyRegistry`, `DatabaseStrategyAccountRouter`, `ModelBlueSizer`
* **Risk**: 5 checks via `RMSEngine`
* **Execution**: `BasketCoordinator` + `OMSService` + `IBKRExecutionAdapter` + `TWSClient` + `OrderSubmitPacer(0.2s)`
* **Persistence**: 17 SQLAlchemy models, 11 repositories, 18 Alembic migrations
* **Safety**: `KillSwitchService`, emergency webhook, `PositionReconciler`, `LivePnlService`
* **Demo**: `PositionBridge` → `PositionStream` (Redis) → SSE `GET /demo/stream` + proxy
* **Frontend**: 6 pages (`AccountsPage`, `PositionsPage`, `AccountSettingsPage`, `SettingsPage`, `SystemMonitorPage`, `ReconcilePage`), Zustand stores, React Query

### Configuration

Inspected `Settings` (18 fields) + `DemoStreamSettings` (9 fields). No `BROKER_MODE` / MockBroker. `extra="ignore"`.

## Documentation Created (New Files)

| # | File | Lines | Source Files |
|---|---|---|---|
| 1 | `docs/architecture/system-architecture.md` | 217 | `app/main.py`, `core/config.py`, `demo_streaming/*`, `db/models/*`, `services/*`, `oms/*`, `rms/*`, `broker/ibkr/*` |
| 2 | `docs/architecture/component-diagram.md` | 170 | Same + `frontend/src/*` |
| 3 | `docs/trading/trading-flow.md` | 215 | `api/routes/webhooks.py`, `services/worker_pool.py`, `order_manager.py`, `rms/engine.py`, `oms/coordinator.py`, `oms/ibkr_adapter.py`, `broker/ibkr/tws_client.py`, `services/pnl.py`, `services/position_reconciler.py` |
| 4 | `docs/trading/rms.md` | 150 | `rms/engine.py`, `rms/checks/*`, `rms/models.py`, `services/order_manager.py` |
| 5 | `docs/trading/oms.md` | 218 | `oms/oms_service.py`, `oms/coordinator.py`, `oms/basket.py`, `oms/models.py`, `services/order_manager.py` |
| 6 | `docs/integrations/ibkr.md` | 230 | `oms/ibkr_adapter.py`, `broker/ibkr/tws_client.py`, `broker/ibkr/scheduler.py`, `oms/submit_pacer.py`, `instruments/*` |
| 7 | `docs/reference/classes.md` | 441 | All app classes |
| 8 | `docs/reference/functions.md` | 393 | Important functions |
| 9 | `docs/reference/api.md` | 412 | `api/routes/*`, `api/router.py`, `demo_streaming/api.py` |
| 10 | `docs/database/database.md` | 566 | `db/models/*`, `db/base.py`, `alembic/*` |
| 11 | `docs/database/er-diagram.md` | 281 | `db/models/*` FKs |
| 12 | `docs/integrations/streaming.md` | 163 | `demo_streaming/*`, `frontend/src/hooks/usePnlStream.ts`, `frontend/src/store/*` |
| 13 | `docs/safety/kill-switch.md` | 386 | `services/kill_switch.py`, `api/routes/config.py` |
| 14 | `docs/safety/emergency-kill-switch.md` | 391 | `api/routes/emergency.py`, `scripts/oms/flatten_gateway_positions.py` |
| 15 | `docs/trading/position-reconciliation.md` | 140 | `services/position_reconciler.py`, `services/reconcile_service.py`, `broker/ibkr/positions.py` |
| 16 | `docs/operations/configuration.md` | 157 | `core/config.py`, `demo_streaming/config.py` |
| 17 | `docs/operations/runtime.md` | 207 | `app/main.py`, `demo_streaming/main.py`, `db/session.py`, `docker-compose.yml` |
| 18 | `docs/operations/failure-recovery.md` | 161 | `services/recovery.py`, `services/worker_pool.py`, `oms/coordinator.py`, `broker/ibkr/tws_client.py`, `services/kill_switch.py` |
| 19 | `docs/testing/testing.md` | 182 | `tests/*`, `conftest.py`, `pyproject.toml` |
| 20 | `docs/diagrams/overall-system.md` | 24 | — |
| 21 | `docs/diagrams/trading-signal.md` | 30 | — |
| 22 | `docs/diagrams/order-execution.md` | 30 | — |
| 23 | `docs/diagrams/ibkr-connection.md` | 32 | — |
| 24 | `docs/diagrams/position-reconciliation-diagram.md` | 20 | — |
| 25 | `docs/diagrams/kill-switch.md` | 26 | — |
| 26 | `docs/diagrams/emergency-kill-switch.md` | 23 | — |
| 27 | `docs/diagrams/gateway-failure.md` | 21 | — |
| 28 | `docs/diagrams/frontend-streaming.md` | 28 | — |
| 29 | `docs/diagrams/account-config-flow.md` | 34 | — |
| 30 | `docs/README.md` (overwritten) | ~220 | Onboarding hub + preserved index |
| 31 | `docs/DOCUMENTATION_AUDIT.md` (this file) | — | — |

**Total new files: 31** (20 markdown docs + 10 diagram files + 1 audit). Pre-existing docs under `docs/` were **not** deleted or overwritten (except `docs/README.md` which was expanded to include the new hub; the original index content is preserved inside it).

## Verification

### Architecture Verified — YES

* Confirmed `app/main.py:31` lifespan order: TWS → OMS → OrderManager → hydrate → connect → Recovery → WorkerPool(10) → Reconciler.
* Confirmed two processes (`:8000` main, `:8010` demo), no CORS/WebSocket on `app.main`.
* Verified `OrderSubmitPacer(0.2s)` is production pacing; `IBKRExecutionScheduler` is tests-only.
* Verified `extra="ignore"` on `Settings` — no `BROKER_MODE`.

### Trading Flow Verified — YES

* Traced `POST /api/webhooks/tradingview` (`webhooks.py:166`) → `compute_idempotency_key` (`worker_pool.py:27`) → `SignalJobRepository.create_job_if_not_exists` → 202.
* Traced `claim_next_jobs` (`worker_pool.py:165`) with `FOR UPDATE SKIP LOCKED` → `_process_claimed_job` → `process_signal_execution` → `parse_inbound_payload` → fan-out → RMS → claim → resolve → `BasketCoordinator.execute` → broker callbacks → persistence.
* Verified lease heartbeat (`worker_pool.py:210`) and fence semantics.
* Verified idempotency across 6 layers (job dedupe, claim, order dedupe, basket uniqueness, event idempotency, lease fence).

### RMS Verified — YES

* Confirmed `get_default_checks()` returns 5 checks numbered 2/3/4/7/8 (`rms/engine.py:18`); checks 1/5/6 absent.
* Verified each check's `evaluate` logic against `rms/checks/*.py`.
* Confirmed `CLOSE` bypasses duplicate/strategy/position/money checks; `EMERGENCY_FLATTEN` bypasses strategy/money checks.

### OMS Verified — YES

* Verified `OMSService` is in-memory, `BasketCoordinator` is DB-backed.
* Verified basket states `PENDING→EXECUTING→OPEN/CLOSED/UNWINDING→COMPENSATED/CRITICAL` (`oms/basket.py:11`).
* Verified retry only on paper ports `7497/4002` (`oms/retry_policy.py:7`) and requires `rms_engine+rms_context` wired.
* Verified `CRITICAL` blocks future OPENs via `is_open_blocked` (`oms/coordinator.py:83`).

### IBKR Integration Verified — YES

* Verified `IBKRExecutionAdapter` (`oms/ibkr_adapter.py:43`) callbacks: `on_order_status` (417), `on_open_order` (521), `on_exec_details` (567), `on_commission_report` (670), `on_error` with `REJECTION_CODES={200,201,10147,10148,10243}` and `NON_TERMINAL_WARNING_CODES={399,2109}`, `on_connection_closed` (778).
* Verified `TWSClient` threaded model (`tws_client.py:16`): `nextValidId`, `connect_and_start`, `request_contract_details`, `request_positions`.
* Verified single `OrderSubmitPacer(0.2s)` on one socket; no multi-gateway pool.
* Checked `paper_execute_stk_as_cfd=true` default (`core/config.py:65`).

### Database Verified — YES

* Inspected all 17 model classes across 13 files; enumerated PK/FK/indexes/constraints/columns.
* Verified composite PKs: `positions (account_id, trade_id)`, `broker_positions (ibkr_account, con_id)`, `per_symbol_limits (symbol, account_id)`.
* Verified migration chain `d4bd73bb4fde → ... → f4a8c2d1e903` (head), 18 versions linear.
* Noted FK-free tables: `instruments`, `execution_claims`, `execution_settings`, `position_reconcile_runs`, `signal_jobs` (has no FK to `accounts`/`signals`).

### Kill Switch Verified — YES

* Verified `_KILL_SWITCH_ACTIVE_ACCOUNTS` in-memory cache (`kill_switch.py:45`) + `_ARMED_STATUSES` tuple (49).
* Verified `KillSwitchService` methods: `initiate_square_off` (156), `arm_account_kill_switch_only` (229), `_flatten_single_position` reversing `signed_qty` (349), `_reconcile_and_finalize` (469).
* Verified arms stay armed after `COMPLETE` until `POST .../kill-switch/clear`.
* Verified kill-switch blocking is pre-RMS in `OrderManager._fanout_single_account` (`order_manager.py:437`).

### Emergency Kill Switch Verified — YES (with caveat)

* Verified EC2 webhook `POST /emergency-kill-switch` (`emergency.py:76`): Bearer `emergency_killswitch_auth_secret` via `hmac.compare_digest`, `func.upper` account resolve, `arm_account_kill_switch_only` (no OMS).
* Verified local script `scripts/oms/flatten_gateway_positions.py` (auth, pacing, verification, exit codes).
* **Caveat**: CSV audit logging for emergency local path was not exhaustively traced to a single canonical script — the EC2 webhook path (the authoritative one) does not write CSV; local flatten scripts may emit CSV via ad-hoc logging. Documented as such.

## Areas That Could Not Be Verified

| Area | Detail |
|---|---|
| Live IBKR Gateway runtime on EC2 | No SSH to the EC2 host; gateway version, IBC config, systemd/tmux wrappers inspected only via local files (`scripts/`, `docker-compose.yml`). Deployment ports are inferred from `Settings` defaults (`7497`), not from a running host. |
| Actual fill latency / throughput | Load-test scripts (`load_test_mft_burst.py`) were read but not executed; no live market data or TWS connection was opened. |
| Frontend build artifact (`frontend/dist`) | `dist/` exists but was not served; verified via `src/` and `vite.config.ts` proxy config. |
| Redis stream contents | Redis not connected during audit; stream semantics verified from `PositionStream` code only. |
| Secrets / `.env` values | Not copied; placeholders used. `webhook_auth_secret` and `emergency_killswitch_auth_secret` existence confirmed via `Settings` fields, not values. |
| `storage/logs/` contents | Not inspected (may contain sensitive or voluminous data). |

## Known Discrepancies

| Discrepancy | Detail |
|---|---|
| `Execution_System_Architecture.md` (repo parent) vs code | Describes 9 RMS checks, multi-process OMS, N Gateways. Code has 5 checks (2/3/4/7/8), single-process OMS on one socket, no multi-gateway. Labeled aspirational in new docs. |
| `backend/POSTMAN_API_TESTING_GUIDE.md` vs routes | Documents `MockBroker`, `/place-order`, `/positions`, `/margin` endpoints that do not exist in `app/api/routes/*`. Labeled stale. |
| `backend/docs/DEVELOPER_EXECUTION_GUIDE.md` vs code | States "Account DB not implemented" — false (`accounts` table + `AccountModel` exist). Labeled stale. |
| `EC2_OPERATIONS_GUIDE.md` vs `Settings` | Mentions `BROKER_MODE` and YAML capital config — not in current `Settings` (`extra="ignore"`). Banner in that file notes staleness; new docs cite `Settings` as truth. |
| Existing `docs/` vs new `docs/` | Old docs (e.g. `backend-api.md`) list endpoints without `/api` prefix in some tables; new `reference/api.md` lists exact mounted paths with prefixes. Substantive content matches; presentation differs. |
| `signal_jobs.account_scope` | Column exists but is unused during ingestion (`_process_tradingview_webhook` leaves it `None`); only used by `fetch_in_flight_accounts` for reconciler visibility. Documented as such. |
| `InstrumentModel` size_increment precision | Model says `Numeric(18,8)` post-`f1b3c5d7e902`; migration widened only `instruments.size_increment`, not qty fields. New `database.md` preserves the per-column distinction. |
| Live PnL marks | `LivePnlService` appears live, but `get_market_data_health` may return `NO_LIVE_PNL_SERVICE` when `TWSClient` is absent (e.g., tests). Documented as conditional. |

## Confidence

| Area | Level | Reason |
|---|---|---|
| Architecture / components / execution path | **HIGH** | Traced call-by-call from `main.py:31` through `webhooks.py:166` → `worker_pool.py:50` → `order_manager.py:85` → `oms/*` → `broker/ibkr/*`, with line-precise citations. |
| RMS rules + OMS basket + IBKR adapter | **HIGH** | Every check's `check_number`/`check_name`/`evaluate` and every basket state / pacer / callback mapping verified. |
| Database schema + migrations | **HIGH** | All model columns/PKs/FKs/indexes enumerated from SQLAlchemy definitions; 18-alembic chain read end-to-end. |
| Kill switch / reconciler / recovery | **HIGH** | `KillSwitchService` globals+methods+API, `PositionReconciler` interval/diffs, `RecoveryManager` requeue logic all line-verified. |
| Config / env vars | **HIGH** | Every `Settings` field traced to its consumer with default/required/sensitivity. |
| Demo streaming / Redis / SSE | **HIGH** | `PositionBridge` poll/fingerprint, `PositionStream` xadd/xread, `demo_streaming/api.py` routes, `usePnlStream` EventSource verified. |
| Frontend pages / routing | **MEDIUM-HIGH** | `App.tsx` routes, `store/pnlStore.ts` + `signalStore.ts`, `pages/*` checked via source reads; no interactive build/run. |
| Deployment / EC2 runtime | **MEDIUM** | Inferred from `Settings` defaults + `scripts/` + `docker-compose.yml` + `EC2_OPERATIONS_GUIDE.md` banner; no live host inspected. |
| Safety / failure recovery edge cases | **MEDIUM-HIGH** | Failure branches checked via `on_error` codes, `on_connection_closed`, `_reclaimer_loop`, `recover_incomplete_baskets`, `CRITICAL` path; timeout races reasoned from code, not load-tested. |

**Overall confidence: HIGH** for architecture/trading/RMS/OMS/broker/database/safety; **MEDIUM** for live deployment specifics that would require EC2 + IBKR connectivity to confirm.

## Verification Method

* No application code was executed beyond file reads and `grep` spot-checks (class counts, route counts, port constants, `check_number` values).
* All `file:line` citations in the new docs were derived from reading the cited files.
* Second pass re-checked class names (`class TWSClient` etc.), API route count (26), `PAPER_IBKR_PORTS`, `OrderSubmitPacer(0.2)` wiring, and RMS numbering against the raw source.

---
*Teams responsible: Verified against commit on `Thu Aug 27 2026`; regenerate after schema or route changes.*

---

# Appendix — Preserved Legacy Docs

The following files pre-existed the audit and were verified as ACCURATE (or labelled STALE/MIXED). They remain in `docs/` unchanged:

- `docs/backend-execution.md` — webhook → RMS → basket → IBKR path
- `docs/backend-map.md` — package tree
- `docs/backend-concurrency.md` — jobs/workers/leases/claims/recovery
- `docs/backend-kill-switch.md` — kill switch detail
- `docs/backend-api.md` — HTTP inventory
- `docs/backend-config.md` — Settings fields
- `docs/backend-persistence.md` — tables/repos/Redis scope
- `docs/backend-rms-oms.md` — RMS/basket/adapter
- `docs/backend-multi-gateway.md` — multi-account vs multi-gateway
- `docs/backend-testing.md` — pytest/ruff
- `docs/frontend.md` — React dashboard
- `docs/conventions.md` — route/DI conventions
- `docs/safety.md` — ports / STK→CFD
- `docs/gaps.md` — not-implemented list
- `docs/EC2_OPERATIONS_GUIDE.md` — EC2 host snapshot

Do not use `backend/POSTMAN_API_TESTING_GUIDE.md` or `backend/docs/DEVELOPER_EXECUTION_GUIDE.md` as source of truth (marked STALE).

---

*End of combined document — 30 sections, generated 2026-08-27. Total source lines: ~5540+ before concatenation.*
