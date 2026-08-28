# IBKR Paper Trading System

## Docs for agents

Start at [`AGENTS.md`](AGENTS.md). In-depth topics: [`docs/`](docs/). Target architecture (not current code): [`../Execution_System_Architecture.md`](../Execution_System_Architecture.md).

## Current status (code, not aspirational)

- **Backend:** FastAPI paper execution — TradingView webhook → Model Blue → RMS (checks 2/3/4/7/8) → basket OMS → IBKR TWS, with Postgres persistence. Multi-account DB routing on **one** Gateway socket; N Gateways + per-gateway rate limits are target-only ([`docs/backend-multi-gateway.md`](docs/backend-multi-gateway.md)).
- **Frontend:** Vite + React TypeScript **live PnL dashboard** (snapshot + SSE). Build with Node ≥ 20; served from `demo_streaming` on `:8010`.
- **Remote dashboard:** `DEMO_STREAM_HOST=0.0.0.0` + AWS SG TCP 8010 → `http://PUBLIC_IP:8010/`. Keep ngrok on `:8000` for TradingView only.
- **Not in main app:** WebSocket, CORS, MockBroker / `BROKER_MODE`, dashboard auth.

## Tech in use

- FastAPI + IBKR TWS API (`ibapi`)
- PostgreSQL (SQLAlchemy / Alembic)
- React + TypeScript (PnL UI)
- Demo stream: Redis Streams + SSE (separate process)

## Features that exist in code

- Connect to IBKR (default paper port `7497`; not live-port blocked)
- TradingView webhook ingestion and execution
- Model Blue sizing / multi-account DB routing
- Paper order execution via OMS + basket coordinator
- Structured logging to `storage/logs/{YYYY-MM-DD}/trading.log` (daily directories)
- Read-only live PnL dashboard (React + HTML fallback on `:8010`)

See [`docs/gaps.md`](docs/gaps.md) for what is explicitly not implemented.
