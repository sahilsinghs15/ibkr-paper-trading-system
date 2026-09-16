# 19-ARCHITECTURE-DECISIONS.md — Key Architectural Decisions (ADRs)

---

## ADR 1: Dual-Process Separation (Ingest vs Trading Backend)
- **Decision**: Run TradingView webhook ingest on port 8000 (Postgres-only) and execution on port 8001.
- **Context**: Inbound alerts must never fail due to IB Gateway drops, rate-limiter stalls, or backend restarts.
- **Why It Must Be Preserved**: Ensures 100% alert capture reliability with zero broker socket coupling.

---

## ADR 2: Single TWS Connection with Multi-Account Tagging
- **Decision**: Use a single `TWSClient` socket connection, tagging `ib_order.account` per order.
- **Context**: Avoids running $N$ gateway instances and simplifies socket lifecycle.
- **Why It Must Be Preserved**: Do not attempt to build a multi-gateway pool without explicit architectural mandate.

---

## ADR 3: Durable Execution Claim Deduplication Barrier
- **Decision**: Engine orders must commit an `execution_claims` row in PostgreSQL before sending an order to IBKR.
- **Context**: In-process sets (`RMSContext.processed_signals`) are lost across process crashes.
- **Why It Must Be Preserved**: Prevents catastrophic order replay on worker restarts.

---

## ADR 4: Two-Phase Commit for Manual Orders
- **Decision**: Insert `manual_orders` row with status `PENDING_SUBMIT` and commit to Postgres before calling `placeOrder()`.
- **Context**: Ensures every broker order is traceable to a durable database ID (`MAN_...`) even if the broker socket drops mid-flight.
- **Why It Must Be Preserved**: Prevents untracked broker orphan orders.

---

## ADR 5: Separate Engine Pair Positions and Manual Lot Positions
- **Decision**: Maintain `positions` (pair-level, Leg A + B) and `manual_positions` (lot-level via `trade_id`) as distinct tables, unifying them in `PositionReconciler`.
- **Context**: Pair strategies require atomic pair-level PnL and compensation, whereas manual trading requires discrete trade lot management.
- **Why It Must Be Preserved**: Do not attempt to merge these ledgers into a single schema without full migration planning.

---

## ADR 6: Kill Switch Armed Status Invariant
- **Decision**: Completing a flatten does not disarm the kill switch. An account remains blocked until explicitly cleared by an operator.
- **Context**: Prevents automated strategies from immediately re-entering positions after an emergency risk exit.
- **Why It Must Be Preserved**: Critical risk control preventing recurring loss loops.
