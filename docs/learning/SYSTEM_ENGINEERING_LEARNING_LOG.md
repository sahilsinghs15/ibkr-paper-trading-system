# System Engineering Learning Log

The engineering history of the IBKR trading system: how it evolved, what broke,
how it was diagnosed, and what was learned.

Read [`README.md`](README.md) first for the sourcing and confidence rules. They
are what make the rest of this trustworthy.

**Repository state this was written against:** commit `64f3009`, 2026-09-21.
224 commits, first commit 2026-08-07.

---

## PART I — UNDERSTANDING THE SYSTEM

| # | Chapter | Covers |
|---|---|---|
| 1 | [System Context](part1_understanding/01_system_context.md) | What the system is, process topology, the two-process split |
| 2 | [Database & Persistence](part1_understanding/02_database_and_persistence.md) | Schema ownership, Alembic, connection pooling |
| 3 | [Signal Ingestion](part1_understanding/03_signal_ingestion.md) | TradingView webhook → `signal_jobs` |
| 4 | [Worker Execution](part1_understanding/04_worker_execution.md) | Claiming, leases, heartbeats, fencing |
| 5 | [Account Routing & Strategy](part1_understanding/05_account_routing_and_strategy.md) | Fan-out, allocations, position sizing |
| 6 | [RMS](part1_understanding/06_rms.md) | Pre-trade risk checks, Cancel Exposure |
| 7 | [OMS & Basket Execution](part1_understanding/07_oms_and_basket_execution.md) | Baskets, retries, naked-pair compensation |
| 8 | [IBKR Integration](part1_understanding/08_ibkr_integration.md) | TWSClient, rate limiter, contracts, market data |
| 9 | [Execution & Positions](part1_understanding/09_execution_and_positions.md) | Fills, position rows, live PnL |

## PART II — SAFETY & OPERATIONS

| # | Chapter | Covers |
|---|---|---|
| 10 | [Kill Switch](part2_safety_operations/10_kill_switch.md) | Scoped flatten, arming, convergence |
| 11 | [Manual Trading](part2_safety_operations/11_manual_trading.md) | Operator orders, idempotency barrier |
| 12 | [Operator Audit](part2_safety_operations/12_operator_audit.md) | `audit_events`, actor attribution, search |
| 13 | [Notifications](part2_safety_operations/13_notifications.md) | Centralized orchestrator, flapping, lifecycle |
| 14 | [Recovery & Reconciliation](part2_safety_operations/14_recovery_and_reconciliation.md) | Broker vs ledger, rogue detection, startup recovery |

## PART III — ENGINEERING HISTORY

| # | Chapter | Covers |
|---|---|---|
| 15 | [Major Production Issues](part3_engineering_history/15_major_production_issues.md) | Case study template; the P0 findings |
| 16 | [Reliability Fixes](part3_engineering_history/16_reliability_fixes.md) | Leases, tasks, retries, watchdog |
| 17 | [Data Integrity Fixes](part3_engineering_history/17_data_integrity_fixes.md) | Attribution, double-counting, ledger truth |
| 18 | [Operational Fixes](part3_engineering_history/18_operational_fixes.md) | UI truthfulness, alert noise, service control |
| 19 | [Architecture Changes](part3_engineering_history/19_architecture_changes.md) | The structural turning points |

## PART IV — OWNERSHIP

| # | Chapter | Covers |
|---|---|---|
| 20 | [Current Architecture](part4_ownership/20_current_architecture.md) | The system as it stands |
| 21 | [Current Known Limitations](part4_ownership/21_current_known_limitations.md) | What is still wrong or unproven |
| 22 | [Important Invariants](part4_ownership/22_important_invariants.md) | What must never break |
| 23 | [Operational Lessons](part4_ownership/23_operational_lessons.md) | Patterns that recur across incidents |
| 24 | [Things I Must Know as Owner](part4_ownership/24_things_i_must_know_as_owner.md) | The short list |

## APPENDICES

| # | Appendix | Covers |
|---|---|---|
| A | [Complete Timeline](appendices/A_complete_timeline.md) | Dated development phases |
| B | [Error Index](appendices/B_error_index.md) | Symptom → chapter lookup |
| C | [Class / Function Map](appendices/C_class_function_map.md) | Where responsibility lives |
| D | [Database Ownership Map](appendices/D_database_ownership_map.md) | Which component writes which table |
| E | [Glossary](appendices/E_glossary.md) | Terms and identifier types |

---

## The through-line

Reading the history end to end, the same lesson recurs in different costumes,
and it is worth stating once at the front.

**Every serious defect in this system has been a state-agreement problem.** Not
an algorithmic error — the position sizing, the PnL maths and the risk formulas
have caused comparatively little trouble. What causes trouble is two places
disagreeing about what is true:

- an in-memory cache and the database row it mirrors
- the database ledger and the actual position at IBKR
- a frontend view and the backend state it renders
- two coroutines that both read "no operation is active" before either wrote
- a test asserting on a code path the system stopped using

The defences that emerged from this are all variations on one idea: **make the
durable store the single source of truth, commit the intent before the side
effect, and derive everything else from it.** The `execution_claims` barrier,
the manual two-phase commit, the intent-first audit recorder, and the flatten
snapshot are the same pattern applied to four different problems.

When you are about to add a cache, a mirror, or a second place that remembers
something, that is the moment to re-read Chapter 23.
