# 01-ARCHITECTURE.md — System Topology & Architecture

---

## 1. MULTI-PROCESS TOPOLOGY

The trading system runs as four decoupled processes:

```
                                  INTERNET / TRADINGVIEW
                                             │
                                             ▼
                                  NGROK / PUBLIC INGEST
                                             │
                       ┌─────────────────────┴─────────────────────┐
                       │                                           │
                       ▼                                           ▼
             [Webhook Ingest API]                        [React UI Dashboard]
               (Port :8000)                                (Vite dev / Dist)
               (app.webhook_ingest)                                │
                       │                                           │
                       │ Writes                                    │ Proxies
                       ▼                                           │ /api/v1  /demo
             ┌───────────────────┐                                 ▼      ▼
             │    PostgreSQL     │◄───────────────────────┐   [Execution API] [Demo SSE]
             │   (Port :5433)    │                        │    (Port :8001)   (Port :8010)
             └───────────────────┘                        │    (app.main)     (demo_stream)
                       ▲                                  │         │               │
                       │ Claims jobs & mutates state      │         │               │
                       └──────────────────────────────────┼─────────┘               │
                                                          │                         │
                                                          ▼                         ▼
                                                  [IB Gateway / TWS]          [Redis PubSub]
                                                  (Live/Paper :4001)           (Port :6379)
```

### Process Roles
1. **Webhook Ingest (`backend/app/webhook_ingest.py`, `:8000`)**:
   - High-availability stateless process.
   - Accepts alerts from TradingView webhooks.
   - Enforces Bearer token authentication (`assert_webhook_auth`).
   - Computes deterministic SHA256 idempotency keys.
   - Durably commits `signal_jobs` rows in PostgreSQL.
   - Never establishes a socket to IBKR.
2. **Trading & Execution Kernel (`backend/app/main.py`, `:8001`)**:
   - Single-instance stateful execution engine.
   - Runs a 10-worker concurrent pool claiming jobs from `signal_jobs`.
   - Executes algorithmic intents through RMS checks, contract resolution, basket coordination, and IBKR execution.
   - Hosts Manual Trading REST APIs (`/api/v1/manual/*`).
   - Hosts admin configuration, kill switch, and position reconcile endpoints.
3. **Demo Streaming Server (`backend/demo_streaming/`, `:8010`)**:
   - Standalone Server-Sent Events (SSE) daemon.
   - Reads PnL, positions, and events from PostgreSQL and Redis.
   - Streams updates to the frontend dashboard.
4. **Frontend SPA (`frontend/`, Vite on `:5173`)**:
   - React 18 + TypeScript user interface.
   - Proxies `/api/v1` to `:8001` and `/demo` to `:8010`.

---

## 2. BACKEND LAYERED ARCHITECTURE

The execution engine is structured in five strictly separated layers:

```
[API Layer]            app/api/routes/*
     │                 HTTP request parsing, schema validation, auth dependencies
     ▼
[Service Layer]        app/services/*
     │                 OrderManager, ManualTradingService, KillSwitchService, LivePnlService
     ├─────────────────┐
     ▼                 ▼
[Domain / RMS / OMS]  [Broker Infrastructure]
app/rms/*             app/broker/*
app/oms/*             TWSClient, GatewayRateLimiter
app/instruments/*     Socket encoding/decoding, token bucket pacing
     │                 │
     ▼                 ▼
[Repository Layer]    [IB Gateway Socket]
app/db/repositories/* TCP port 4001
     │
     ▼
[Database Layer]
PostgreSQL tables via SQLAlchemy asyncpg
```

---

## 3. COMPONENT INVENTORY & RESPONSIBILITIES

### Core Entrypoints
- `app/main.py`: Lifespan startup, dependency initialization, background worker spawning.
- `app/webhook_ingest.py`: Webhook gateway application.

### Key Packages
- `app/api/`: FastAPI routes and authentication dependencies (`deps.py`).
- `app/services/`: Business workflows and lifecycle orchestrators.
- `app/rms/`: Algorithmic risk management engine (`engine.py`) and checks (`checks/`).
- `app/oms/`: Multi-leg basket coordinator (`coordinator.py`), OMS service (`oms_service.py`), IBKR execution adapter (`ibkr_adapter.py`).
- `app/broker/ibkr/`: Low-level broker integration (`tws_client.py`, `gateway_rate_limiter.py`).
- `app/instruments/`: Contract resolver (`resolver.py`) and instrument catalog.
- `app/accounts/`: Multi-account router (`router.py`) and configuration service (`config_service.py`).
- `app/db/models/`: Declarative ORM models.
- `app/db/repositories/`: Data access repositories.

---

## 4. SYSTEM NETWORK BOUNDARIES

| Host / Port | Protocol | Usage | Process | External Facing? |
|---|---|---|---|---|
| `127.0.0.1:8000` | HTTP/REST | TradingView webhook ingestion | `webhook_ingest.py` | Yes (via Ngrok/Reverse Proxy) |
| `127.0.0.1:8001` | HTTP/REST | Trading execution & config APIs | `main.py` | Local only (never bind 0.0.0.0) |
| `127.0.0.1:8010` | HTTP/SSE | Demo streaming PnL dashboard | `demo_streaming` | Optional public IP via SG |
| `127.0.0.1:5173` | HTTP | Vite dev server | Node.js | Local dev only |
| `127.0.0.1:4001` | IB API (TCP)| TWS / IB Gateway socket | IB Gateway | Local loopback only |
| `127.0.0.1:5433` | PostgreSQL | Application database | Postgres 17 (Docker) | Local loopback only |
| `127.0.0.1:6379` | Redis | PubSub for demo streaming | Redis | Local loopback only |
