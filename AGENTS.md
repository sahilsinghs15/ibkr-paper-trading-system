# Agent map — IBKR paper trading app

**Verified from:** `backend/app/main.py`, `backend/app/webhook_ingest.py`, `backend/app/api/routes/*`, `backend/app/core/config.py`, `backend/demo_streaming/*`, `frontend/src/*`.

Two FastAPI processes: **webhook ingest** (`app.webhook_ingest:app` on `:8000`, Postgres-only) receives TradingView alerts into `signal_jobs`; **trading/execution** (`app.main:app` on `:8001`) runs a 10-worker pool through Model Blue → RMS (checks 1/2/3/4/7/8/101) → basket OMS → IBKR TWS, with execution claims, crash recovery, and kill-switch support. Ingest stays up when Gateway bounces or the trading app restarts. Multi-account routing tags `ib_order.account` on **one** TWS/Gateway socket (`GatewayRateLimiter` ~30 msg/sec, P0 flatten reserve). N IB Gateways and per-gateway rate limits are **not** built — [`docs/backend-multi-gateway.md`](docs/backend-multi-gateway.md). The Vite React app under `frontend/` is a live PnL dashboard plus a **Settings** page for RMS limits and capital allocation. It is served by a separate demo SSE process on port **8010** (not by `app.main`).

Do **not** treat [`../Execution_System_Architecture.md`](../Execution_System_Architecture.md) as current code. Do **not** use [`backend/POSTMAN_API_TESTING_GUIDE.md`](backend/POSTMAN_API_TESTING_GUIDE.md) as an API inventory (it documents endpoints that do not exist).

## Which doc to open

| Task | Doc |
|------|-----|
| Debug a missed / rejected / incomplete fill | [`docs/backend-execution.md`](docs/backend-execution.md) |
| Package tree / where to change code | [`docs/backend-map.md`](docs/backend-map.md) |
| Jobs, workers, leases, claims, recovery | [`docs/backend-concurrency.md`](docs/backend-concurrency.md) |
| Kill switch / emergency flatten / IBKR leftover flatten script | [`docs/backend-kill-switch.md`](docs/backend-kill-switch.md) |
| Gateway socket drop / reconnect / parked baskets | [`docs/runbooks/gateway-failure.md`](docs/runbooks/gateway-failure.md) |
| Exact HTTP endpoints | [`docs/backend-api.md`](docs/backend-api.md) |
| Env / Settings fields | [`docs/backend-config.md`](docs/backend-config.md) |
| Tables, repos, in-memory vs DB | [`docs/backend-persistence.md`](docs/backend-persistence.md) |
| RMS checks / basket / TWS adapter | [`docs/backend-rms-oms.md`](docs/backend-rms-oms.md) |
| Multi-account vs multi-gateway / rate limits | [`docs/backend-multi-gateway.md`](docs/backend-multi-gateway.md) |
| pytest / ruff | [`docs/backend-testing.md`](docs/backend-testing.md) |
| React PnL dashboard + demo UI | [`docs/frontend.md`](docs/frontend.md) |
| How to add a route / DI rules | [`docs/conventions.md`](docs/conventions.md) |
| Paper vs live ports, STK→CFD | [`docs/safety.md`](docs/safety.md) |
| What is **not** implemented | [`docs/gaps.md`](docs/gaps.md) |
| Watchdog (monitoring, Telegram, recovery) | [`docs/watchdog.md`](docs/watchdog.md) |
| Doc index | [`docs/README.md`](docs/README.md) |

## Run / test

```bash
# Webhook ingest (TradingView / ngrok — Postgres only, no IBKR)
cd /home/tradingapp/app/backend
uv sync --extra dev   # or use existing .venv
.venv/bin/uvicorn app.webhook_ingest:app --host 127.0.0.1 --port 8000

# Trading / execution API (local only)
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8001

# Tests / lint
.venv/bin/pytest
.venv/bin/ruff check app/ tests/ scripts/

# Read-only PnL dashboard (needs Postgres + Redis; Node ≥20 to build React)
.venv/bin/python -m demo_streaming
# open http://127.0.0.1:8010/
# remote: DEMO_STREAM_HOST=0.0.0.0 … then http://PUBLIC_IP:8010/ (SG TCP 8010)

# Vite React dashboard (proxies /demo → :8010)
cd /home/tradingapp/app/frontend
npm install && npm run build   # optional: serve dist from :8010
npm run dev                    # http://127.0.0.1:5173/

# Production run path is systemd units (not process_manager.py):
#   trading-backend.service  (:8001)
#   webhook-ingest.service   (:8000)
#   ibgateway.service
# process-manager.service must stay disabled.
```

Runtime logs: trading app `storage/logs/{YYYY-MM-DD}/trading.log`; webhook ingest `webhook.log`; demo stream `demo.log`. Do not write `/home/tradingapp/logs`.

## Hard invariants

- `POST /api/webhooks/tradingview` on **ingest** (`:8000`) returns **HTTP 202** with status **`accepted`** and enqueues `signal_jobs`; workers on **trading** (`:8001`) run `process_signal_execution` asynchronously. Durable payload is `signal_jobs.capture_data`.
- Trading app lifespan: TWS → OMS → OrderManager → hydrate → **CriticalRecoveryService** (wired to `BasketCoordinator`) → connect → **RecoveryManager** → **ExecutionWorkerPool(10)** on `app.state.worker_pool`.
- **Execution claims** are acquired after RMS + instrument resolve, before broker submit — the durable dedupe barrier across crashes/workers.
- Kill switch **stays armed** after flatten completes until operator `POST .../kill-switch/clear`. Account daily stop/target auto-exit uses the same kill switch (`requested_by=auto_risk`).
- Pair auto-exit uses `positions.target` / `stop` / `time_limit` (and units), gated by `positions.exit_automation_enabled`. Stop/target are **signed PnL levels**: stop fires at `pnl <= stop`, target at `pnl >= target` (negatives and 0 are valid). Allocation edits of target/stop apply to newly opened pairs only; OPEN pairs can be edited via the Open Positions popup (`PATCH .../positions/{trade_id}/exits`). Toggling allocation automation also arms or disarms currently OPEN pairs of that strategy. Master switch `RISK_EXIT_MONITOR_ENABLED` defaults false.
- Remainder-retry is allowed on live Gateway **4001** after M9/M14 identity/persist fixes (`paper_retry_ports_allowed` includes 4001).
- **BASKET_CRITICAL** auto-recovery: background flatten via `CriticalRecoveryService`, unlock OPENs when broker snapshot flat (`BasketState.RECOVERED`); dashboard banner via `GET /api/v1/baskets/critical`.
- Production submit pacing is `GatewayRateLimiter` (~30/24/6 msg/sec, wait+timeout, Error 100 cooldown) on the single adapter — one limiter, one socket, all accounts. P1 `placeOrder`/`cancelOrder` may consume tokens without a normal-bucket token if that would not eat the P0 emergency reserve.
- Multi-Gateway pool / per-gateway limiter: **not implemented**. Socket reconnect-on-drop **is** implemented on the single TWS client. Do not describe `ibkr_account` as a Gateway mapping.
- Trading app HTTP surface (`:8001`): health, orders, and **config CRUD** under `/api/v1/config/*`. Webhooks live on ingest (`:8000`) only. No CORS, WebSocket, or static files on either app.
- Default IBKR port is **4001** (live Gateway).
- There is **no** `BROKER_MODE` / MockBroker in `Settings`. Extra env keys are ignored (`extra="ignore"`).
- Redis is used only by `demo_streaming`, not by the main trading app.
- Do not bind the trading app to `0.0.0.0`. Keep ngrok on ingest `:8000` only; use `:8010` for the dashboard (optionally `DEMO_STREAM_HOST=0.0.0.0`).
- `TRADINGAPP_TESTING=1` is pytest-only. Never set it on trading-backend or any other `placeOrder` process.

## Ignore / do not treat as source of truth

| Path | Why |
|------|-----|
| `backend/POSTMAN_API_TESTING_GUIDE.md` | Historical; lists APIs and schemas that no longer exist |
| `backend/docs/DEVELOPER_EXECUTION_GUIDE.md` | Stale human guide — prefer `app/docs/` |
| `../Execution_System_Architecture.md` | Target design, not current code |
| `broker/ibkr/scheduler.py` | **Removed** — live pacing is `GatewayRateLimiter` in `broker/ibkr/gateway_rate_limiter.py` |

## Source of truth (code)

- Entrypoints: `backend/app/webhook_ingest.py` (ingest), `backend/app/main.py` (trading)
- Config: `backend/app/core/config.py`
- Routes: `backend/app/api/routes/{health,webhooks,orders,config}.py`
- Queue/workers: `backend/app/services/worker_pool.py`
- Pipeline: `backend/app/services/order_manager.py`
- Recovery: `backend/app/services/recovery.py`
- Kill switch: `backend/app/services/kill_switch.py`
- Risk-exit monitor: `backend/app/services/risk_exit_monitor.py` + `risk_exit_rules.py`
- RMS: `backend/app/rms/engine.py` + `backend/app/rms/checks/`
- OMS / basket: `backend/app/oms/`
- DB models: `backend/app/db/models/`
- Demo UI server: `backend/demo_streaming/`

---

# Engineering Responsibility and Verification Rules

## 1. Agent Responsibility — Think, Don't Just Execute

The implementation agent is responsible for understanding the INTENTION and GOAL of every task.

A prompt is an instruction and design proposal, but it is NOT an unquestionable source of truth.

The agent must NOT blindly follow individual implementation steps if those steps conflict with:

- the actual task objective
- the existing architecture
- existing source-of-truth boundaries
- type safety
- runtime correctness
- database integrity
- security
- trading safety
- established repository conventions

The agent must interpret the meaning of the task first.

The priority should be:

```
TASK GOAL / INTENTION
        ↓
EXISTING SYSTEM ARCHITECTURE
        ↓
SAFETY / CORRECTNESS
        ↓
PROMPT'S IMPLEMENTATION GUIDANCE
        ↓
INDIVIDUAL IMPLEMENTATION STEPS
```

The steps in a prompt are guidance for achieving the goal.

If the prompt contains a small mistake, incomplete assumption, outdated file reference, incorrect implementation detail, or suboptimal approach, the agent must recognize it instead of mechanically implementing it.

The agent should use engineering judgment.

For example:

If a prompt says:

> "Modify file A to achieve X"

but repository inspection shows that the correct source of truth is actually file B, the agent should not blindly modify A.

It should:

1. understand why X is required
2. inspect the actual architecture
3. identify the correct implementation location
4. implement X at the correct boundary
5. explain the deviation in the final report

Do NOT blindly obey implementation mechanics at the expense of the actual objective.

## 2. Read Before Modifying

Before making implementation changes:

1. Read AGENTS.md.
2. Read relevant engineering documentation.
3. Inspect the existing architecture.
4. Locate the actual source of truth.
5. Trace relevant callers/dependencies.
6. Understand existing tests.
7. Understand existing configuration and conventions.
8. Only then modify code.

Do not make changes based solely on a filename or assumption from the prompt.

Do not assume that the file mentioned in a prompt is necessarily the correct implementation boundary.

## 3. Root-Cause-First Debugging

When an error, warning, failing test, type-checking issue, lint issue, or runtime problem is encountered:

DO NOT immediately apply the smallest possible change just to make the diagnostic disappear.

Instead:

1. Read the affected file.
2. Read the surrounding code.
3. Understand the data flow.
4. Understand why the diagnostic exists.
5. Identify the root cause.
6. Check for related errors caused by the same underlying issue.
7. Fix the underlying problem.
8. Re-run the relevant checks.
9. Inspect the resulting diagnostics again.
10. Continue until the file and related implementation are genuinely correct.

The objective is NOT:

> "make this one error disappear"

The objective is:

> "make the implementation correct."

## 4. Full-File Investigation Rule

When a checker reports an error in a file, do not assume that the reported diagnostic is the only issue in that file.

For example:

> Pyright reports 1 error in file.py

Do NOT simply fix that one line and move on.

Instead:

1. Read the entire relevant file.
2. Understand the surrounding functions/classes.
3. Check related types and control flow.
4. Run Pyright again.
5. Inspect ALL remaining diagnostics for that file.
6. Fix the root cause and related issues.
7. Repeat until clean.

The same rule applies to Ruff.

If Ruff reports one warning in a file:

- Read the relevant file properly.
- Understand why the pattern exists.
- Check for related violations.
- Fix the underlying code where appropriate.
- Run Ruff again.
- Verify the entire file.

Never assume:

> "I fixed the reported error, therefore the file is clean."

## 5. Pyright Verification Rule

Python implementation work must be verified with Pyright according to the repository's configured command/configuration.

After implementation changes:

1. Run Pyright.
2. Identify every affected file.
3. For every affected file with diagnostics, inspect the file properly.
4. Understand the root cause.
5. Fix the root cause.
6. Re-run Pyright.
7. Continue until no relevant Pyright errors remain.

Do NOT stop after the first successful partial fix.

Do NOT rely on the IDE showing fewer errors.

The command-line checker is part of the verification process.

When practical, run the configured repository-wide Pyright check after the targeted checks are clean.

## 6. Ruff Verification Rule

Python implementation work must also be verified with Ruff according to the repository's configured command/configuration.

After implementation changes:

1. Run Ruff.
2. Identify every affected file with violations.
3. Read the relevant file and understand the violation.
4. Determine whether multiple reported issues share the same root cause.
5. Fix the underlying code.
6. Re-run Ruff.
7. Continue until the relevant implementation is clean.

Do NOT fix only the first Ruff warning and assume the file is finished.

If the same file produces additional diagnostics after the first fix, inspect those diagnostics as part of the same debugging cycle.

## 7. Do Not Use Blind Suppressions

Do NOT solve type-checking or linting problems by blindly adding:

```
# type: ignore
# pyright: ignore
# noqa
noqa directives
arbitrary casts
broad Any types
disabling rules
weakening configuration
```

just to make the checker pass.

Before using a suppression, determine:

1. Why the checker reports the issue.
2. Whether the implementation itself is incorrect.
3. Whether the type/model/interface is inaccurate.
4. Whether the code can be structured correctly without suppression.
5. Whether the external library's type definition is genuinely inaccurate.

A suppression may be appropriate when the external type information is demonstrably incorrect or there is a legitimate boundary that cannot be represented accurately.

If a suppression is necessary:

- keep it as narrow as possible
- use the specific diagnostic/rule
- add a concise explanation when appropriate
- do not use broad suppression
- verify that unrelated diagnostics are still visible

Example of bad behavior:

> "Pyright complains, so ignore the entire file."

Example of acceptable behavior:

> "The runtime library accepts a value that its upstream stub incorrectly declares as a narrower type. The runtime behavior is verified, and a narrowly scoped suppression is used at that boundary."

## 8. Iterative Verification Loop

After implementation, use this general loop:

```
IMPLEMENT
    ↓
RUN CHECKS
    ↓
COLLECT ALL DIAGNOSTICS
    ↓
GROUP RELATED ERRORS
    ↓
READ AFFECTED FILES
    ↓
IDENTIFY ROOT CAUSE
    ↓
FIX ROOT CAUSE
    ↓
RUN CHECKS AGAIN
    ↓
INSPECT ALL NEW DIAGNOSTICS
    ↓
REPEAT
    ↓
CLEAN RESULT
```

Do not use this inefficient approach:

```
Fix error #1
    ↓
Run checker
    ↓
Discover error #2
    ↓
Fix error #2
    ↓
Run checker
    ↓
Discover error #3
    ↓
...
```

When multiple diagnostics are present in the same file, inspect the file and diagnostics together and address the underlying cause and related issues in one reasoning cycle where possible.

This does NOT mean blindly changing everything at once.

It means understanding the whole affected area before making the fix.

## 9. Verify the Whole Change, Not Just the Last Error

A task is not complete simply because the last reported error disappeared.

After implementation, verify:

- modified Python files with Pyright
- modified Python files with Ruff
- relevant tests
- relevant type checks
- relevant build checks
- relevant frontend checks when frontend code changed
- migrations/schema consistency when database changes occurred
- runtime behavior when appropriate

The final verification should cover the complete implementation, not only the line that previously failed.

## 10. Warnings Are Signals, Not Noise

Do not automatically ignore warnings.

For every warning encountered, determine whether it represents:

- a real bug
- a type-safety issue
- a maintainability issue
- a code-quality issue
- a deprecated API
- an unsafe assumption
- an actual intentional exception

If it is a real issue, fix it.

If it is intentionally acceptable, understand why before leaving it.

Do not hide warnings merely to produce a clean command output.

## 11. Test Failure Rule

When a test fails:

Do not immediately modify the test just to make it pass.

First determine whether:

- production code is wrong
- test expectations are wrong
- test data/setup is wrong
- the test is outdated
- a type/interface mismatch exists
- the implementation violates the intended behavior

Fix the actual root cause.

Tests must not be weakened simply to obtain a green test suite.

## 12. Preserve Existing Architecture

Before changing an existing subsystem, understand:

- source of truth
- ownership boundaries
- data flow
- persistence model
- concurrency model
- API boundaries
- external integrations
- existing invariants

Do not introduce a second competing implementation when an existing authoritative mechanism already exists.

Prefer integrating with the existing architecture over creating parallel logic.

## 13. Trading System Safety

This repository contains trading/execution/risk functionality.

Extra caution is required when modifying:

- OMS
- orders
- executions
- positions
- broker integrations
- risk management
- kill switches
- reconciliation
- account state

Never trade correctness for implementation convenience.

Never bypass an existing invariant simply to make a test or checker pass.

Never make external broker calls while unnecessarily holding database locks/transactions.

Never introduce notification behavior into the execution-critical path.

## 14. Scope Discipline

Only modify what is required for the task.

Do not opportunistically refactor unrelated code merely because you encountered it.

However, if an unrelated-looking issue is directly connected to the root cause of the current task, it may need to be fixed.

Use engineering judgment and clearly report such changes.

## 15. Final Verification Requirement

Before declaring a task COMPLETE, the agent must perform a final verification pass.

The final pass must answer:

- Did the intended functionality work?
- Did the modified Python files pass Pyright?
- Did the modified Python files pass Ruff?
- Did relevant tests pass?
- Did the implementation introduce new warnings/errors?
- Were any diagnostics suppressed?
- If so, why?
- Were existing invariants preserved?
- Were unrelated files changed?
- Were migrations/builds/type checks required and verified?

Do not claim COMPLETE based only on:

> "The code was changed."

Completion means:

> "The intended behavior was implemented and the implementation was properly verified."

## 16. Final Reporting

At the end of a task, report:

### Implementation
What was actually changed.

### Reasoning / Architecture
Important implementation decisions and why they were made.

### Verification
Exact checks performed, such as:

- Pyright
- Ruff
- pytest
- mypy
- TypeScript
- ESLint
- build
- migration verification
- runtime verification

### Diagnostics
Any remaining warnings/errors.

### Suppressions
Any `ignore`/`noqa`/type suppression added and the reason.

### Deviations
If the implementation differs from the prompt's suggested steps, explain why.

### Status

```
COMPLETE
INCOMPLETE
BLOCKED
```

Do not claim COMPLETE if meaningful verification remains unfinished.

## 17. Most Important Rule

The agent is an ENGINEERING AGENT, not a command executor.

Do not optimize for:

> "follow every sentence of the prompt literally."

Optimize for:

> "understand the requested outcome and implement it correctly, safely, cleanly, and verifiably within the existing architecture."

Prompts provide requirements, constraints, context, and suggested approaches.

The agent must still think.

If a prompt contains a mistake:

- identify it
- understand the intended goal
- choose the technically correct approach
- implement the goal safely
- document the deviation

Never knowingly implement an incorrect approach simply because it appeared as a step in the prompt.

## 18. Avoid Unnecessary Type Conversions (Performance & Pyrefly `unnecessary-type-conversion`)

Unnecessary wrapping of an already-typed value in `int()`, `bool()`, `str()` (and likewise `float()` on an already-`float`) is a defect, not style. It adds runtime overhead on hot paths, obscures the true `Settings` type, and triggers Pyrefly `unnecessary-type-conversion` (`argument is already of type <T>`). Settings fields in `backend/app/core/config.py` are already correctly typed — do not re-cast them.

**Rule:** Never wrap a `Settings` (or other already-typed) value when the target parameter already expects that type. Consult `Settings` field types first; only convert when the types genuinely differ.

Bad — `Settings` field is already `int`/`bool`/`str`:

```python
# backend/app/core/config.py: risk_exit_max_retries: Annotated[int, Ge(0)] = 3
max_retries=int(settings.risk_exit_max_retries)        # unnecessary int() — Pyrefly: argument is already int
enabled=bool(settings.risk_exit_monitor_enabled)        # unnecessary bool() — already bool
host=str(settings.ibkr_host)                            # unnecessary str() — already str
max_age=int(settings.margin_snapshot_max_age_sec)       # unnecessary — already int
```

Good:

```python
max_retries=settings.risk_exit_max_retries
enabled=settings.risk_exit_monitor_enabled
host=settings.ibkr_host
max_age=settings.margin_snapshot_max_age_sec
```

When conversion *is* needed, it is explicit and type-changing (e.g., `float(settings.ibkr_connection_timeout)` where `ibkr_connection_timeout: int` but the API expects `float` for `timeout`). `int -> float`, `Decimal -> float`, or parsing external `str` input are legitimate; `int -> int`, `bool -> bool`, `str -> str`, `float -> float` are not.

**Verification:** After any `Settings` usage, run Pyrefly with the `unnecessary-type-conversion` rule enabled and fix all occurrences of `int()`, `bool()`, `str()` (and `float()`) where the argument type already matches. Prefer `backend/.venv/bin/pyrefly check --warn unnecessary-type-conversion` or a project-wide `pyrefly check` that surfaces this diagnostic. Fix the root cause (remove the wrapper) instead of suppressing.
