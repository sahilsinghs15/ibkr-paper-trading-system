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
