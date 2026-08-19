# Docs index

**Verified from:** live routers under `backend/app/api/`, `backend/demo_streaming/api.py`, and the files linked below.

Agent entrypoint: [`../AGENTS.md`](../AGENTS.md).

## Not current (do not copy as inventory)

| Document | Role |
|----------|------|
| [`../../Execution_System_Architecture.md`](../../Execution_System_Architecture.md) | **Target** architecture (multi-process OMS, nine RMS checks, dashboard CRUD, kill switch). Not a description of this FastAPI app. |
| [`../backend/POSTMAN_API_TESTING_GUIDE.md`](../backend/POSTMAN_API_TESTING_GUIDE.md) | Historical Postman notes; documents MockBroker / place-order / positions / margin routes that **do not** exist in code. |
| [`../backend/docs/DEVELOPER_EXECUTION_GUIDE.md`](../backend/docs/DEVELOPER_EXECUTION_GUIDE.md) | Older human guide; some “not implemented” bullets are stale. Prefer this `app/docs/` tree. |

## Which file to open

| If you need… | Open |
|--------------|------|
| Debug webhook → fill path / log greps | [`backend-execution.md`](backend-execution.md) |
| Exact HTTP methods and paths | [`backend-api.md`](backend-api.md) |
| Every `Settings` / demo env field | [`backend-config.md`](backend-config.md) |
| Postgres tables, Alembic, Redis scope | [`backend-persistence.md`](backend-persistence.md) |
| RMS check list, basket states, TWS | [`backend-rms-oms.md`](backend-rms-oms.md) |
| How to run tests | [`backend-testing.md`](backend-testing.md) |
| Vite scaffold + demo HTML UI | [`frontend.md`](frontend.md) |
| Adding routes / DI conventions | [`conventions.md`](conventions.md) |
| Paper ports, STK→CFD override | [`safety.md`](safety.md) |
| Explicit not-implemented list | [`gaps.md`](gaps.md) |
