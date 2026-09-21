# Chapter 1 — System Context

## Current Implementation

### What this system is

A multi-account order and execution management system that turns TradingView
webhook alerts into orders at Interactive Brokers, and gives an operator direct
control over positions when the automation must be overridden.

It is not a strategy engine. Signal generation happens in TradingView; this
system decides whether a signal is *safe* to execute, how large it should be per
account, and how to get it filled without leaving one leg of a pair naked.

### Process topology

Four separate processes, deliberately not one.

| Process | Port | Owns | Touches the broker? |
|---|---|---|---|
| Webhook ingest (`app/webhook_ingest.py`) | 8000 | Inserting into `signal_jobs` | No |
| Trading backend (`app/main.py`) | 8001 | Everything: RMS, OMS, positions, kill switch | Yes — the only one |
| Demo streaming (`demo_streaming/`) | 8010 | SSE position/PnL feed to the frontend | No |
| Watchdog (`scripts/watchdog_main.py`) | — | Health monitoring, automated recovery | No |

**The ingest/trading split is the oldest deliberate architectural decision in
the system** and is recorded as ADR 1 in
[`docs/engineering/19-ARCHITECTURE-DECISIONS.md`](../../engineering/19-ARCHITECTURE-DECISIONS.md).
The reasoning: an inbound alert must never be lost because the IB Gateway
dropped, the rate limiter stalled, or the trading backend was restarting. Ingest
speaks only to Postgres, so the only thing that can stop it capturing an alert
is Postgres itself being down.

The cost of that decision is that ingest and execution are decoupled in time.
A signal can sit in `signal_jobs` for a while before a worker claims it, which
is why the Red Zone gate (Chapter 6) and the OPEN-before-CLOSE ordering
(Chapter 4) exist at the claiming layer rather than at the HTTP layer.

### Concurrency model

This is the single most misunderstood part of the system, and the review
material is blunt about it
([`docs/review/BUGS-concurrency.md`](../../review/BUGS-concurrency.md),
2026-09-03):

> Every "per-gateway" and "distributed" question in the brief collapses to
> "one process, one event loop, one reader thread, plus a separate ingest
> process that only inserts into `signal_jobs`".

Concretely, inside the `:8001` process:

- **One asyncio event loop.** All service code, HTTP handlers and workers.
- **One TWS reader thread** (`TWSClientThread`, started in
  `app/broker/ibkr/tws_client.py`). IBKR callbacks arrive on *this* thread, not
  the loop.
- **One `TWSClient` socket** and **one `GatewayRateLimiter`**, both constructed
  in `app/main.py`.

The thread boundary is where a disproportionate share of the system's hardest
bugs have lived. Any object reachable from both the loop and the reader thread
is a candidate for a race, and an `asyncio.Lock` held on the loop is invisible
to the reader thread. See Chapter 16.

### Database

PostgreSQL, accessed through SQLAlchemy async with asyncpg. The runtime engine
is pooled (`pool_size=20`, `max_overflow=30`, `pool_timeout=30`,
`pool_recycle=1800`); under test it is `NullPool`
(`app/db/session.py`). Alembic is the sole schema authority — 47 migrations as
of 2026-09-21.

### Failure behavior

The system is built to fail *closed* on the trading path and *open* on the
capture path:

- Ingest keeps accepting alerts when the broker is unreachable.
- The trading backend refuses to open positions when it cannot establish that
  doing so is safe — no broker connection, kill switch armed, Red Zone, RMS
  reject, audit trail unavailable.

The asymmetry is intentional. A missed alert is recoverable; an unintended
position is not.

### Current limitations

- A single TWS socket means all accounts share one rate limiter. One account
  flooding orders can starve another (Risk 2 in
  [`18-KNOWN-RISKS.md`](../../engineering/18-KNOWN-RISKS.md)).
- Multi-gateway is described as a *target* architecture in
  [`docs/backend-multi-gateway.md`](../../backend-multi-gateway.md), not a
  current one. ADR 2 explicitly says not to build a gateway pool without a
  mandate.

---

## Engineering History

### Chronology

| Date | Change | Commit |
|---|---|---|
| 2026-08-07 | Initial commit | `3a9d00f` |
| 2026-08-09 | Candle strategy pipeline, simulation runner, mock broker | `0bb6ded`, `b39f459` |
| 2026-08-14 | **Strategy-based broker system replaced by OMS + RMS** | `902ca4d` |
| 2026-08-17 | Alembic introduced; Postgres becomes the schema authority | `86485a6` |
| 2026-08-18 | Baskets introduced for multi-leg atomicity | `6bc05f4` |
| 2026-08-21 | Durable webhook queuing with async worker pool | `4c47e37` |
| 2026-08-31 | Independent watchdog service | `8cbe353` |
| 2026-09-01 | systemd-native service management replaces `process_manager` | `394e616`, `82252f1` |
| 2026-09-03 | Structured review produces `docs/review/` findings | `448a9e2` |
| 2026-09-11 | Manual trading system | `837baf2` |
| 2026-09-17 | Centralized notification system | `42a36cd` |
| 2026-09-18 | Operator audit trail replaces legacy audit logs | `5b9d094` |

**Confidence: Confirmed** — all entries are git commits with these dates.

### The shape of the evolution

Three distinct phases are visible in the commit history, and they explain why
the codebase looks the way it does.

**August 7–14 — prototype.** A candle strategy, a mock broker, a simulation
runner. Almost none of this survives; `902ca4d` on 2026-08-14 replaced the
strategy-based broker system wholesale with OMS/RMS. If you find code that
looks like it belongs to a different system, check whether it predates this
commit.

**August 17 – September 3 — durability.** Alembic, baskets, execution claims,
the worker pool, the watchdog. The recurring theme is moving state out of
process memory and into Postgres. This phase ends with the 2026-09-03 review,
which went looking for the places where that migration was incomplete — and
found 10 P0 findings across three reports.

**September 3 – 21 — operator control and truthfulness.** Manual trading,
scoped kill switch, notifications, audit trail. The theme shifts from "does the
system execute correctly" to "can an operator see what it did and intervene".

### Case studies

None in this chapter. System-context changes appear as architecture changes in
[Chapter 19](../part3_engineering_history/19_architecture_changes.md).
