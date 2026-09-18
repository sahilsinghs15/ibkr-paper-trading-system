# 15-LOGGING-OBSERVABILITY.md — Logging, Audit & Observability

---

## 1. APPLICATION LOGGING

- **Configured via**: [`backend/app/core/logger.py`](file:///home/dev3/Documents/ibkr-paper-trading-system/backend/app/core/logger.py#L1).
- **Log Locations**:
  - Trading Engine: `storage/logs/{YYYY-MM-DD}/trading.log`
  - Webhook Ingest: `webhook.log`
  - Demo Streaming: `demo.log`
- **Context Binding**: Use `bind_log_context(signal_id=..., trade_id=..., account_id=...)` to automatically attach correlation identifiers to every log record.

---

## 2. DATABASE AUDIT & EVENT JOURNALS

1. **`audit_events` Table — Operator Audit Trail (authoritative)**:
   - One row per meaningful human/operator (or external-caller) action that can affect trading, risk,
     configuration, inventory, emergency state, services or security. Not UI analytics.
   - Written only by backend routes through `app/audit/recorder.py` (`AuditRecorder`):
     - `transaction()` — settings mutations: audit row commits atomically with the change; refusals are
       recorded separately after rollback.
     - `operation()` — side-effecting actions (manual orders, Close Pair, flattens, inventory fixes, service
       lifecycle): a `PENDING` row is **committed before** the side effect, then finalized once.
       `trading-backend` stop/restart can never run without a durable audit row (fails closed with 503).
     - `record_committed()` — security events (login success/failure, logout, ended-session token reuse,
       access denied; the last two are throttled).
   - Taxonomy (`app/audit/taxonomy.py`): category / action / result / actor_type are structured columns.
   - Actor = server-derived identity (JWT + `auth_sessions`) + observed context (IP resolved by uvicorn
     trusted-proxy handling, User-Agent, client-reported device id/app version flagged unverified).
     Never taken from request bodies. Does not claim to identify the physical person.
   - **Append-only enforced in PostgreSQL**: DELETE/TRUNCATE rejected; UPDATE only allowed to finalize a
     `PENDING` row once, touching outcome columns only. No foreign keys (evidence survives deletions).
   - Values are sanitized before persistence (`app/audit/sanitize.py`): secrets redacted, control chars
     stripped, sizes bounded.
   - Read API (admin only): `GET /api/v1/audit/events|events/{id}|facets`. UI: Audit Logs page.
   - Legacy operator rows from `event_log` / `manual_audit_events` were imported with `provenance=LEGACY_*`
     (no IP/session/role fabricated).
2. **`auth_sessions` Table**: one row per login; its id is the signed `sid` JWT claim. Logout ends the session
   and every token (including derived SSE tokens) carrying that `sid` is rejected.
3. **`event_log` Table — System Event Journal**:
   - Machine/engine events (RMS outcomes, reconcile sweeps, kill switch operations, service events) and the
     notification mirror. Shown in the "System Event Journal" tab (`GET /demo/event-journal`).
   - It is **not** the operator audit trail; do not add operator attribution writes here.
4. **`manual_audit_events` Table**: manual order lifecycle ledger (submissions, cancels, status updates, fills).

### Client IP trust model
Browser → dashboard (`:8010`, peer IP is the real client; forwarded headers only trusted from 127.0.0.1)
→ `/api/v1` proxy **replaces** any client-supplied `X-Forwarded-For`/`X-Real-IP`/`Forwarded` with its own
view of the peer → trading API (`:8001`, loopback only) trusts `X-Forwarded-For` only from 127.0.0.1.

## 3. TELEGRAM OPERATOR ALERTS

Handled by [`backend/app/services/notification_canonical.py`](file:///home/dev3/Documents/ibkr-paper-trading-system/backend/app/services/notification_canonical.py#L1):
- Dispatches high-priority messages to the configured Telegram chat.
- **Trigger Events**:
  - Kill Switch activated or cleared.
  - Position reconciliation detects rogue trades (confirmed on 2 consecutive sweeps).
  - Account realized loss threshold breached.
  - TWS connection drops and fails to reconnect.

---

## 4. WHAT MUST NEVER BE LOGGED

- Sensitive authentication tokens or raw password hashes.
- Full unmasked API keys or secrets.
- PII (Personally Identifiable Information).
