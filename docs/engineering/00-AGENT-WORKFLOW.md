# 00-AGENT-WORKFLOW.md — Mandatory Agent Operating Lifecycle

> **GOVERNANCE LEVEL: MANDATORY**  
> Every coding agent or software engineer operating on this repository must execute the following lifecycle in sequence. Skipping steps requires explicit written justification in the final report.

---

## 1. THE MANDATORY AGENT LIFECYCLE

```
                 RECEIVE TASK
                      │
                      ▼
               READ AGENTS.md
                      │
                      ▼
        READ RELEVANT ENGINEERING DOCS
       (docs/engineering/01-19 as needed)
                      │
                      ▼
              INSPECT REPOSITORY
       (Read callers, models, repositories)
                      │
                      ▼
          FIND EXISTING IMPLEMENTATION
             (Can this be reused?)
                      │
                      ▼
               IDENTIFY OWNER
          (Which service/class owns it?)
                      │
                      ▼
            TRACE CALL / DATA FLOW
         (Trace API -> Service -> DB/Broker)
                      │
                      ▼
            IDENTIFY SOURCE OF TRUTH
            (What is authoritative?)
                      │
                      ▼
             IDENTIFY INVARIANTS
       (Constraints, locks, validation rules)
                      │
                      ▼
            CHECK EXISTING TESTS
            (What tests already prove it?)
                      │
                      ▼
             PLAN MINIMAL CHANGE
            (Smallest safe architectural delta)
                      │
                      ▼
                  IMPLEMENT
            (Strictly in the correct layer)
                      │
                      ▼
               TARGETED TESTS
           (Unit test the exact new logic)
                      │
                      ▼
             STATIC DIAGNOSTICS
           (Ruff, mypy, tsc --noEmit)
                      │
                      ▼
              REGRESSION TESTS
        (Run full/related pytest suites)
                      │
                      ▼
            LOGICAL VERIFICATION
        (Prove math, locks, dedupe keys)
                      │
                      ▼
            RUNTIME VERIFICATION
          (Where applicable / safe)
                      │
                      ▼
                REVIEW DIFF
         (git diff: check for unintended edits)
                      │
                      ▼
          REVIEW DATA & SIDE EFFECTS
           (Ensure no broker/DB corruption)
                      │
                      ▼
             FINAL VERIFICATION
         (Satisfy all completion checks)
                      │
                      ▼
             DECLARE COMPLETE
```

---

## 2. STOP CONDITIONS (WHEN TO PAUSE & INVESTIGATE)

An agent must **IMMEDIATELY STOP AND INVESTIGATE** if any of the following occur:
1. **Unclear Ownership**: You cannot determine whether `OrderManager`, `ManualTradingService`, or `BasketCoordinator` owns a mutation.
2. **Unclear Source of Truth**: You are unsure whether a value comes from PostgreSQL, an in-memory cache, or the broker socket.
3. **Unexpected Architecture**: You encounter code that contradicts your assumption (e.g., finding that CFD ticks derive marks from underlying STK contracts).
4. **Unexpected Broker Mutation**: Any action that might place, cancel, or modify an order at IBKR without explicit instruction.
5. **Migration Uncertainty**: You are unsure whether a column change requires data backfilling or defaults.
6. **Unexplained Test Failure**: A test fails for reasons you cannot trace. Never comment out a test or weaken assertions.
7. **Contradictory Data**: Two models or tables report divergent quantities for the same instrument without an explanation.

**UNDER STOP CONDITIONS**:
- **DO NOT GUESS**.
- **DO NOT SILENTLY OVERWRITE CODE**.
- Inspect callers, read git history (`git log -S`), and verify existing tests.

---

## 3. ANTI-HALLUCINATION & FACTUALITY DISCIPLINE

In all thoughts, plans, and final reports, you must explicitly distinguish between four levels of knowledge:
- **OBSERVED**: Directly read from repository source code, schema, or system logs.
- **VERIFIED**: Proven by passing tests, diagnostic commands, or mathematical derivation.
- **INFERRED**: Deducted from related architectural patterns but not explicitly stated.
- **ASSUMED**: Unverified hypothesis. **Assumed behavior must NEVER be written into code as fact.**

If something cannot be confirmed from the codebase, explicitly state **UNKNOWN**.

---

## 4. MANDATORY COMPLETION PROTOCOL

An agent may only declare a task **COMPLETE** by providing this exact structured report:

```markdown
## Implementation Summary
- What exact changes were made? (Files modified, lines changed, classes added/edited).

## Architectural Compliance
- Why is this the correct architectural layer and owner?
- What existing infrastructure was reused? (e.g., TWSClient, LivePnlService, GatewayRateLimiter).

## Source of Truth & Data Ownership
- What data was read? What was written? Which table/model is authoritative?

## Invariants Preserved
- What critical invariants were maintained? (e.g., filled_qty <= qty, deduplication, row locking).

## Automated Test Evidence
- Commands executed: `pytest path/to/test.py`
- Results: Exact pass/fail counts.

## Diagnostic Evidence
- `ruff check`: Result.
- `mypy`: Result.
- `tsc --noEmit`: Result.

## Runtime & Logical Verification
- Mathematical proof of PnL/quantity transitions.
- Confirmation of lock order to prevent deadlocks.

## Diff & Safety Review
- Output of `git diff --stat`.
- Confirmation that no secrets were committed.
- Confirmation that no live broker orders were placed.

## Remaining Risks & Unknowns
- Any limitations, untestable edge cases, or follow-ups.

## Final Status
[COMPLETE / INCOMPLETE / BLOCKED]
```
