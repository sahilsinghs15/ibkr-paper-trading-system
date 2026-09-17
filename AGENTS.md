# AGENTS.md — Master Agent Governance & Operating Rules

> **CRITICAL MANDATE FOR ALL AI AGENTS AND ENGINEERS**  
> This repository is a production High-Frequency/Medium-Frequency Trading (HFT/MFT) and Order Management System (OMS) interfacing with Interactive Brokers (IBKR). Correctness, determinism, financial safety, and architectural integrity supersede speed, cleverness, or cosmetic convenience. You are strictly forbidden from guessing, refactoring out of preference, hallucinating APIs, or bypassing existing safety barriers.

---

## 1. PURPOSE

This document is the supreme governance contract for any automated coding agent or human engineer operating within this codebase. It establishes operational constraints, ownership rules, architectural invariants, and safety boundaries to prevent state corruption, financial loss, double execution, and drift.

---

## 2. MANDATORY FIRST STEP (BEFORE TOUCHING CODE)

Before writing or editing a single line of code in this repository:
1. **READ THIS DOCUMENT (`AGENTS.md`)** completely.
2. **READ RELEVANT ENGINEERING DOCS** in [`docs/engineering/`](docs/engineering/):
   - Workflow & Verification: [`docs/engineering/00-AGENT-WORKFLOW.md`](docs/engineering/00-AGENT-WORKFLOW.md)
   - Architecture & Layers: [`docs/engineering/01-ARCHITECTURE.md`](docs/engineering/01-ARCHITECTURE.md)
   - Code Structure & Ownership: [`docs/engineering/02-CODE-STRUCTURE.md`](docs/engineering/02-CODE-STRUCTURE.md)
   - Data Ownership & Source of Truth: [`docs/engineering/03-DATA-OWNERSHIP.md`](docs/engineering/03-DATA-OWNERSHIP.md)
   - Order & Execution Lifecycles: [`docs/engineering/05-ORDER-LIFECYCLE.md`](docs/engineering/05-ORDER-LIFECYCLE.md), [`06-EXECUTION-LIFECYCLE.md`](docs/engineering/06-EXECUTION-LIFECYCLE.md)
   - Risk, RMS & Kill Switch: [`docs/engineering/10-RISK-SAFETY.md`](docs/engineering/10-RISK-SAFETY.md)
   - Testing & Diagnostic Protocol: [`docs/engineering/14-TESTING-DIAGNOSTICS.md`](docs/engineering/14-TESTING-DIAGNOSTICS.md)
3. **INSPECT EXISTING CODE**: Trace actual call paths, callers, and dependencies.
4. **IDENTIFY THE SINGLE OWNER**: Locate the exact service and repository responsible for the domain concern.
5. **IDENTIFY INVARIANTS & CONSTRAINTS**: Check database constraints, locks, and RMS checks.
6. **VERIFY EXISTING TESTS**: Review tests proving invariants in `backend/tests/`.
7. **PLAN THE SMALLEST SAFE DELTA**: Never implement directly from a prompt or summary.

---

## 3. ARCHITECTURE-FIRST & REUSE RULE

- **Never duplicate logic**: Search the repository before writing a class, method, helper, hook, or validator.
- If a capability exists in [`OrderManager`](backend/app/services/order_manager.py), [`ManualTradingService`](backend/app/services/manual_trading.py), [`LivePnlService`](backend/app/services/pnl.py), [`PositionReconciler`](backend/app/services/position_reconciler.py), [`GatewayRateLimiter`](backend/app/broker/ibkr/gateway_rate_limiter.py), [`InstrumentResolver`](backend/app/instruments/resolver.py), or [`TWSClient`](backend/app/broker/ibkr/tws_client.py), **YOU MUST REUSE IT**.
- Never create parallel broker pathways, shadow PnL calculations, or redundant contract resolvers.

---

## 4. OWNERSHIP RULE

Every business action and mutation must have exactly one authoritative owner:
- **Engine Algorithmic Orders**: Owned by [`OrderManager`](backend/app/services/order_manager.py) $\rightarrow$ [`BasketCoordinator`](backend/app/oms/coordinator.py) $\rightarrow$ [`OMSService`](backend/app/oms/oms_service.py).
- **Manual Orders**: Owned by [`ManualTradingService`](backend/app/services/manual_trading.py) $\rightarrow$ [`ManualOrderRepository`](backend/app/db/repositories/manual_repository.py).
- **Manual Fills & Position Mutation**: Owned exclusively by [`ManualExecutionListener`](backend/app/services/manual_callbacks.py) $\rightarrow$ [`ManualPositionRepository`](backend/app/db/repositories/manual_repository.py).
- **Engine Positions**: Owned by [`OrderManager._update_runtime_state`](backend/app/services/order_manager.py) $\rightarrow$ [`PositionRepository`](backend/app/db/repositories/position_repository.py).
- **Broker Interface**: Owned exclusively by [`TWSClient`](backend/app/broker/ibkr/tws_client.py).
- **Outbound Pacing**: Owned exclusively by [`GatewayRateLimiter`](backend/app/broker/ibkr/gateway_rate_limiter.py).
- **Real-Time Mark & Unrealized PnL**: Owned exclusively by [`LivePnlService`](backend/app/services/pnl.py).
- **Reconciliation & Anomaly Detection**: Owned exclusively by [`PositionReconciler`](backend/app/services/position_reconciler.py).
- **Emergency Flattening**: Owned exclusively by [`KillSwitchService`](backend/app/services/kill_switch.py).

---

## 5. SOURCE-OF-TRUTH RULE

Distinguish strictly between authoritative state, external reality, and derived/cached views:
- **Authoritative Database State**:
  - Inbound queue: `signal_jobs`
  - Deduplication barrier: `execution_claims`
  - Orders: `orders` (Engine), `manual_orders` (Manual)
  - Positions: `positions` (Engine pairs), `manual_positions` (Manual lots)
  - Risk states: `kill_switch_operations`, `accounts.trading_paused`, `manual_halt_state`
- **External Reality**:
  - Physical broker positions: IBKR `position()` callback $\rightarrow$ `broker_positions` snapshot.
  - Execution fills: IBKR `execDetails()` callback $\rightarrow$ `executions`, `manual_executions`, `trade_executions`.
  - Market prices: IBKR `tickPrice()` stream.
- **Derived / Ephemeral Views (Never Treat as Authoritative)**:
  - Frontend React state / Zustand stores.
  - `demo_streaming` SSE caches and Redis pub/sub.
  - `positions.live_pnl` and `manual_positions.live_pnl` (derived from marks).
  - In-memory hot caches (e.g., `_KILL_SWITCH_ACTIVE_ACCOUNTS`).

---

## 6. DO-NOT-BYPASS CRITICAL INFRASTRUCTURE

Under no circumstances may any agent bypass:
1. **[`TWSClient`](backend/app/broker/ibkr/tws_client.py)**: Exactly one socket connection. Never instantiate a second client or raw socket.
2. **[`GatewayRateLimiter`](backend/app/broker/ibkr/gateway_rate_limiter.py)**: All outbound calls must pass through `acquire()`. Never call `placeOrder` directly without a rate limiter token.
3. **[`execution_claims`](backend/app/db/repositories/execution_claim_repository.py)**: Engine orders must acquire a durable claim in PostgreSQL *before* submitting to IBKR.
4. **Manual Idempotency Barrier**: `manual_orders` unique constraint on `(account_id, idempotency_key)` must be checked and committed before placing a manual order.
5. **[`RMSEngine`](backend/app/rms/engine.py)**: Sequential evaluation of checks 1, 2, 3, 4, 7, 8, 101 must precede every engine order.
6. **[`KillSwitchService`](backend/app/services/kill_switch.py)**: Active kill switch must block all new `OPEN` orders across all paths.
7. **[`SessionClock`](backend/app/services/session_clock.py)**: Global Red Zone gating must never be ignored for non-emergency orders.
8. **[`InstrumentResolver`](backend/app/instruments/resolver.py)**: CFD-to-underlying mapping and size increment rounding must be preserved.

---

## 7. BROKER SAFETY

- **NEVER place unrequested broker orders**: Do not run live order tests against real or paper gateways without explicit user instruction.
- **NEVER cancel live broker orders** outside defined test mocks or explicit user requests.
- **NEVER mutate positions directly at the broker**: All position changes must originate from validated orders.
- **CFD Contract Rules**: All CFD orders must set `secType="CFD"`, `outsideRth=False`, `eTradeOnly=False`, and `firmQuoteOnly=False`.
- **Order ID Allocation**: Order IDs must be allocated via `client.allocate_next_order_id()`. Never hardcode, guess, or default order ID to 1.
- **Testing Guard**: Never set `TRADINGAPP_TESTING=1` on production processes or live order processes.

---

## 8. DATABASE SAFETY

- **Alembic is the Sole Schema Authority**: All schema mutations must use versioned Alembic migrations under `backend/alembic/versions/`.
- **`Base.metadata.create_all()` is FORBIDDEN** in production application code.
- **Never wipe or truncate tables**: Do not delete historical rows from `orders`, `executions`, `positions`, `trade_executions`, `event_log`, or `manual_audit_events` to satisfy tests or clear UI errors.
- **Row-Level Locking**: Mutations of position rows must use `select(...).with_for_update()`.
- **No Broker Calls inside DB Transactions**: Never hold a database transaction open while awaiting an IBKR network round-trip.

---

## 9. IDENTITY MODEL SAFETY

Strictly honor identifier scopes and types:
- `account_id` (BigInt): Internal database surrogate key. Never send to IBKR.
- `ibkr_account` (String, e.g. `DU123456`): Authoritative IBKR account code.
- `internal_order_id` (String): Antigravity order ID (`ORD-...` or `MAN-...`). Passed as `orderRef` to IBKR.
- `broker_order_id` (Integer): Ephemeral session order ID from `allocate_next_order_id()`. Invalid across gateway restarts.
- `perm_id` (BigInt): Permanent broker order ID from IBKR. Valid across restarts.
- `trade_id` (String): Economic position/lot identifier. **NEVER REUSE A CLOSED TRADE_ID**.
- `exec_id` (String): Unique broker execution fill ID. Must be processed exactly once.
- `con_id` (BigInt): IBKR contract identifier. Never confuse CFD `con_id` with underlying STK `con_id`.
- **Never correlate entities by symbol alone** when stronger identities (`con_id`, `trade_id`, `perm_id`) exist.

---

## 10. BUSINESS LOGIC LOCATION

- **FastAPI Routes (`app/api/routes/*`)**: Validation of HTTP request models, invoking services, returning response schemas. No direct SQL or business calculations.
- **Services (`app/services/*`)**: Pre-trade safety gates, order orchestration, PnL calculations, transaction lifecycles.
- **RMS (`app/rms/*`)**: Pure risk checks against `RMSContext`. No database mutations inside check classes.
- **Repositories (`app/db/repositories/*`)**: Atomic queries, upserts, locking, constraint checking. No broker interactions.
- **Broker Adapters (`app/broker/*`)**: Socket management, rate limiting, low-level packet encoding/decoding.

---

## 11. FRONTEND RULES

- **The frontend is presentation only**: Never trust state, validations, or calculations from the React UI.
- All risk checks, price tick interpretations, margin checks, and permission checks **MUST be performed on the backend**.
- Frontend components must gracefully handle SSE disconnects and display stale-data warnings rather than synthesizing artificial numbers.

---

## 12. TESTING & VERIFICATION RULES

- Every bug fix or feature **MUST be accompanied by automated tests** in `backend/tests/` proving the invariant.
- Mocks must accurately reflect IBKR asynchronous behavior (separate reader thread, out-of-order `commissionReport` vs `execDetails`).
- Tests must clean up after themselves and must not leave database locks or hanging threads.

---

## 13. DIAGNOSTICS & COMPLETION LADDER

Work is NOT complete simply because code was written or syntax passes. You must verify:
1. **Static Analysis**: `ruff check app/ tests/` (no regressions).
2. **Type Checking**: `mypy app/` and `cd frontend && npx tsc --noEmit`.
3. **Automated Unit & Integration Tests**: `pytest` covering modified files and critical paths.
4. **Logical Invariant Verification**: Confirm mathematical formulas, constraints, and locks.
5. **Runtime Verification**: Verify via live logs or local test client where applicable.

---

## 14. DEFINITION OF DONE

An agent may only report a task as **COMPLETE** by fulfilling the mandatory completion protocol in [`docs/engineering/00-AGENT-WORKFLOW.md`](docs/engineering/00-AGENT-WORKFLOW.md#mandatory-completion-protocol).

---

## 15. THE UNKNOWN RULE

If any requirement, data flow, contract mapping, or historical reason is unclear:
**STOP. INSPECT. TRACE. VERIFY.**
Do not guess. Do not assume. Explicitly state `UNKNOWN` and request clarification or investigate deeper.

---

## 16. GIT & SECRET SAFETY

- **Destructive Git Operations Forbidden**: Never run `git reset --hard`, `git clean -fd`, force push, or checkout commands that discard user work.
- **Never Commit Secrets**: Never write passwords, tokens, private keys, or credentials into repository files.

---

## 17. DOCUMENTATION INDEX

All agents must consult the detailed engineering documentation:
| Doc | Purpose |
|---|---|
| [`00-AGENT-WORKFLOW.md`](docs/engineering/00-AGENT-WORKFLOW.md) | Mandatory step-by-step agent operating lifecycle and stop conditions |
| [`01-ARCHITECTURE.md`](docs/engineering/01-ARCHITECTURE.md) | Multi-process design, system topology, layers, and network boundaries |
| [`02-CODE-STRUCTURE.md`](docs/engineering/02-CODE-STRUCTURE.md) | Class responsibilities, file organization, and logic placement rules |
| [`03-DATA-OWNERSHIP.md`](docs/engineering/03-DATA-OWNERSHIP.md) | Comprehensive source-of-truth matrix across all models |
| [`04-DOMAIN-MODELS.md`](docs/engineering/04-DOMAIN-MODELS.md) | In-depth analysis of Domain entities, relationships, and invariants |
| [`05-ORDER-LIFECYCLE.md`](docs/engineering/05-ORDER-LIFECYCLE.md) | Detailed trace of Engine and Manual order state machines |
| [`06-EXECUTION-LIFECYCLE.md`](docs/engineering/06-EXECUTION-LIFECYCLE.md) | Broker callbacks $\rightarrow$ DB $\rightarrow$ Position $\rightarrow$ PnL flow |
| [`07-POSITION-PNL.md`](docs/engineering/07-POSITION-PNL.md) | Engine pair vs Manual lot accounting, average cost, and PnL math |
| [`08-IBKR-BROKER.md`](docs/engineering/08-IBKR-BROKER.md) | TWSClient, threading, rate limiter, and reconnect mechanics |
| [`09-CONTRACT-MARKET-DATA.md`](docs/engineering/09-CONTRACT-MARKET-DATA.md) | Contract resolution, CFD vs STK, tick processing, and effective marks |
| [`10-RISK-SAFETY.md`](docs/engineering/10-RISK-SAFETY.md) | RMS 7 checks, Kill Switch, Red Zone, and Trading Pause mechanics |
| [`11-DATABASE-ALEMBIC.md`](docs/engineering/11-DATABASE-ALEMBIC.md) | PostgreSQL models, repositories, transactions, and migration rules |
| [`12-API-BACKEND.md`](docs/engineering/12-API-BACKEND.md) | FastAPI routing, auth dependencies, error handling, and schemas |
| [`13-FRONTEND.md`](docs/engineering/13-FRONTEND.md) | React UI pages, components, hooks, Zustand stores, and SSE |
| [`14-TESTING-DIAGNOSTICS.md`](docs/engineering/14-TESTING-DIAGNOSTICS.md) | Strict diagnostic ladder, testing practices, and completion reporting |
| [`15-LOGGING-OBSERVABILITY.md`](docs/engineering/15-LOGGING-OBSERVABILITY.md) | Logging conventions, audit trails, and Telegram notifications |
| [`16-WORKERS-SCHEDULERS.md`](docs/engineering/16-WORKERS-SCHEDULERS.md) | Worker pool, background sweepers, and schedulers |
| [`17-GIT-DEPLOYMENT.md`](docs/engineering/17-GIT-DEPLOYMENT.md) | Git safety, deployment runbooks, systemd units, and rollbacks |
| [`18-KNOWN-RISKS.md`](docs/engineering/18-KNOWN-RISKS.md) | Real architectural risks, historical bugs, and edge cases |
| [`19-ARCHITECTURE-DECISIONS.md`](docs/engineering/19-ARCHITECTURE-DECISIONS.md) | Immutable architectural decisions and rationale |

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
