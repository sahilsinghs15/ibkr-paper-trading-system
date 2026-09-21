# Chapter 3 — Signal Ingestion

## Current Implementation

### Responsibility

Accept TradingView webhook alerts and make them durable. Nothing else. The
ingest process does not evaluate risk, does not talk to IBKR, and does not
decide whether a signal will execute.

### Major components

| Component | File | Role |
|---|---|---|
| Ingest app | `backend/app/webhook_ingest.py` | FastAPI app on port 8000 |
| Webhook route | `backend/app/api/routes/webhooks.py` | Payload validation |
| Job repository | `backend/app/db/repositories/signal_repository.py` | `signal_jobs` writes |
| Parser | `backend/app/services/model_blue/parser.py` | Payload → `OrderIntent` |

### Call path

```text
TradingView alert
  → POST /webhook (port 8000)
    → validate payload
      → persist raw payload to disk (non-blocking)
        → INSERT INTO signal_jobs (status='PENDING')
          → 200 to TradingView
```

The response is returned once the row is committed. Execution happens later, in
a different process.

### Database tables

- `signal_jobs` — the inbound queue and the durability boundary
- `signals` — the parsed, business-level signal record

### State transitions

`signal_jobs.status`: `PENDING → CLAIMED → PROCESSING → {PROCESSED, REJECTED,
RECOVERY_REQUIRED, QUARANTINED}`

Claiming and the fencing rules are Chapter 4's subject.

### Authentication

Controlled by `WEBHOOK_AUTH_ENABLED` / `WEBHOOK_AUTH_SECRET`. It is currently
disabled in the local `.env`; the emergency kill-switch webhook has its own
separate secret and is enabled.

### Failure behavior

Ingest is deliberately the most available part of the system. It stays up when
the broker is down, when the trading backend is restarting, and when the Red
Zone is blocking execution. Signals accumulate in `signal_jobs` and are executed
or rejected later, with the reason recorded.

### Current limitations

- Ordering between an OPEN and its matching CLOSE is enforced at *claim* time,
  not at ingest time. See finding C4 in Chapter 4.
- Payload capture to disk was made non-blocking (`4c47e37`); if the filesystem
  is unwritable the alert is still queued, but the raw capture may be missing.

---

## Engineering History

### Chronology

| Date | Change | Commit |
|---|---|---|
| 2026-08-12 | Webhook support added to backend API | `25165db` |
| 2026-08-13 | Persistent file capture of raw TradingView payloads | `2540028` |
| 2026-08-18 | Original webhook payloads persisted to the database | `0d688a2` |
| 2026-08-21 | **Durable queuing with async worker pool; non-blocking disk logging** | `4c47e37` |
| 2026-08-24 | Optional webhook authentication toggle | `61571ca` |
| 2026-09-10 | Admin-only Ingest Feed page to inspect raw `signal_jobs` | `8864e41` |

**Confidence: Confirmed** (git history).

### Why ingest is a separate process

The decision is recorded as ADR 1. The commit sequence shows it was not a
day-one design: webhooks initially arrived at the main backend (`25165db`,
2026-08-12) and the durable queue with a worker pool arrived nine days later
(`4c47e37`, 2026-08-21). The intervening period is when baskets, Alembic and the
OMS were introduced — that is, once the execution path grew enough moving parts
to fail, decoupling capture from execution became necessary.

This is the clearest example in the repository of a pattern worth internalising:
**the durability boundary moved earlier over time.** First the payload was
captured to disk, then to the database, then into a queue with explicit status
transitions. Each step moved the "we definitely have this alert" point further
from the broker.

### Case studies

No ingestion-specific case study is recorded. The signal *display* defect from
2026-08-21 (`a869a69`, "resolve signal monitor and signal tray data
disappearance bug") is an operational/UI issue and is discussed in
[Chapter 18](../part3_engineering_history/18_operational_fixes.md).
