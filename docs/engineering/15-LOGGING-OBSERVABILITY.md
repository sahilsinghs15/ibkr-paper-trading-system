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

## 2. DATABASE AUDIT LOGGING

1. **`event_log` Table**:
   - Central append-only audit trail for algorithmic engine events.
   - Records RMS check outcomes, kill switch activations, reconcile sweeps, and position align actions.
   - Enforces unique `idempotency_key` where applicable.
2. **`manual_audit_events` Table**:
   - Dedicated audit trail for manual trading actions.
   - Records `MANUAL_ORDER_SUBMITTED`, `MANUAL_ORDER_CANCELLED`, `MANUAL_ORDER_STATUS_UPDATED`, and `MANUAL_EXECUTION_RECEIVED`.

---

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
