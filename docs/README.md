# Docs index

**Verified from:** live routers under `backend/app/api/`, `backend/demo_streaming/api.py`, and the files linked below.

Agent entrypoint: [`../AGENTS.md`](../AGENTS.md).

Do not treat any document in this tree as self-certified ACCURATE. Read the code, or [`review/MAP.md`](review/MAP.md).

## Not current (do not copy as inventory)

| Document | Role |
|----------|------|
| `Execution_System_Architecture.md` (parent of repo, not in git tree) | **Target** architecture (multi-process OMS, nine RMS checks, etc.). Not a description of this FastAPI app. |
| [`../backend/POSTMAN_API_TESTING_GUIDE.md`](../backend/POSTMAN_API_TESTING_GUIDE.md) | Historical; documents an API that does not exist. Do not follow. |
| [`../backend/docs/DEVELOPER_EXECUTION_GUIDE.md`](../backend/docs/DEVELOPER_EXECUTION_GUIDE.md) | Older human guide; stale. Prefer this `app/docs/` tree. |
| [`archive/production_mft_ibkr_pacing.md`](archive/production_mft_ibkr_pacing.md) | Competing pacing numbers. Live limiter is `GatewayRateLimiter`. |

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
| Live Gateway 4001, STK→CFD, webhook auth | [`safety.md`](safety.md) |
| Explicit not-implemented list | [`gaps.md`](gaps.md) |
| EC2 host snapshot (SSH / IBC / never-do) | [`EC2_OPERATIONS_GUIDE.md`](EC2_OPERATIONS_GUIDE.md) |
| Watchdog (monitoring, Telegram, recovery) | [`watchdog.md`](watchdog.md) |
| Gateway socket drop / reconnect / parked baskets | [`runbooks/gateway-failure.md`](runbooks/gateway-failure.md) |
