# Operator Runbook: Telegram Alerts & `/status` Command

> [!IMPORTANT]
> **READ THIS FIRST FOR LIVE TRADING OPERATORS**
> - **Observer Notice**: Watchdog is an independent health observer that monitors system components and sends Telegram alerts. System supervision is managed by Linux `systemd` services. Watchdog does not start or restart trading services.
> - **Service Health vs. Trading Status**: A service can be **technically healthy** while trading is temporarily **blocked for safety**. For example, `PostgreSQL: SERVICE STATUS HEALTHY / TRADING STATUS BLOCKED` means the database is working normally, but trading is paused by a separate safety condition (such as host RAM usage or an active kill switch). It does **not** mean PostgreSQL is broken or crashed.
> - **What `RECOVERED` Means**: `RECOVERED` means a monitored condition or safety gate has returned to normal. It does **not** mean a physical service was restarted or recovered from a crash.

---

## Quick Reference Card

| Visual Indicator | Meaning | Immediate Operator Action |
| :--- | :--- | :--- |
| 🟢 **GREEN** | System or service is operating normally | No action required |
| 🟡 **YELLOW** | Expected stop outside trading hours, or mild resource warning | Check session time; monitor if during market hours |
| 🟠 **ORANGE** | Trading safety condition is preventing execution | Issue `/status` in Telegram; investigate safety reason |
| 🔴 **RED** | Serious component failure or critical resource condition | Follow Level 3 escalation procedure; do **not** bypass safety |

### Common Status Terminology
- **`HEALTHY`**: Component responded successfully to health checks.
- **`MARKET_CLOSED`**: Expected state outside US trading hours (Mon–Fri 09:30–16:00 ET).
- **`TRADING_BLOCKED`**: Safety checks prevent new trades from executing.
- **`RECOVERED`**: Monitored safety gate or health metric returned to normal.
- **`FAILED`**: Component failed to respond to health checks during active monitoring.

---

## 1. Telegram Message Structure Explained

Every Telegram alert follows a structured format designed to answer four core questions:
1. **What component is affected?** (`SERVICE`)
2. **Is the component physically working?** (`SERVICE STATUS`)
3. **Can the system execute trades right now?** (`TRADING STATUS`)
4. **Why did this alert fire?** (`DETAILS`)

### Standard Alert Layout

```text
🚨 WATCHDOG — TRADING BLOCKED

SERVICE
PostgreSQL

SERVICE STATUS
HEALTHY

TRADING STATUS
BLOCKED

STATUS
TRADING_BLOCKED

EVENT
TRADING_BLOCKED

DETAILS
Trading safety gate failed: system-monitor CRITICAL [RAM]: RAM usage critical (90.6%)

RECOVERY
Trading remains BLOCKED until safety gates pass and operator clears.
```

### Field Breakdown

| Field Name | Example Value | Plain-English Meaning |
| :--- | :--- | :--- |
| **Header Banner** | `🚨 WATCHDOG — TRADING BLOCKED` | Quick visual summary of the event severity. |
| **`SERVICE`** | `PostgreSQL` | The specific system component being evaluated. |
| **`SERVICE STATUS`** | `HEALTHY` | The physical health of the component itself (`HEALTHY`, `DEGRADED`, `FAILED`, `MARKET_CLOSED`). |
| **`TRADING STATUS`** | `BLOCKED` | System-wide trading execution status (`READY`, `BLOCKED`, `NOT AFFECTED`, `MARKET CLOSED`). |
| **`STATUS`** | `TRADING_BLOCKED` | Internal state machine status code. |
| **`EVENT`** | `TRADING_BLOCKED` | The transition event that triggered this notification. |
| **`DETAILS`** | `system-monitor CRITICAL [RAM]...` | Human-readable explanation of why the alert fired. |
| **`RECOVERY`** | `Trading remains BLOCKED...` | Next step or recovery state overview. |

---

## 2. Alert Severity Guide & Meanings

| Alert State | Visual Emoji | Meaning | Required Operator Action |
| :--- | :---: | :--- | :--- |
| **`HEALTHY`** | 🟢 | Service is working normally. | No action required. |
| **`RECOVERED`** | 🟢 | Previous alert or safety gate has cleared. | No action required unless other alerts remain active. |
| **`DEGRADED`** | 🟡 | Component is alive but operating with warning metrics. | Issue `/status` in Telegram; monitor during trading session. |
| **`TRADING_BLOCKED`** | 🟠 | A safety gate is preventing trade execution. | Issue `/status`; inspect safety gate details; do **not** force trades. |
| **`MARKET_CLOSED`** | 🟡 | Service intentionally stopped outside trading hours. | Expected behavior. No action required. |
| **`CRITICAL`** | 🔴 | Serious resource condition ($\ge 90\%$) or service failure. | Check `/status`; notify technical lead if persistent during market hours. |
| **`FAILED`** | 🔴 | Service failed health check or TCP port is closed. | Escalate if during active trading hours (Mon–Fri 09:30–16:00 ET). |

---

## 3. Trading Window & Market-Closed Semantics

The system enforces strict US equities session awareness:
- **Active Trading Window**: Monday through Friday, **09:30 to 16:00 ET** (America/New_York timezone).
- **Equivalent India Time (IST)**: 19:00 to 02:30 IST (or 18:30 to 02:00 IST during Daylight Saving Time).

### Normal State Outside Trading Hours

Outside the active session, the system intentionally stops session-dependent components to conserve resources and prevent unnecessary TWS connectivity errors:

| Component | Status Outside Session | Is This Normal? | Explanation |
| :--- | :--- | :---: | :--- |
| **IB Gateway** | `MARKET_CLOSED` | **YES** | Gateway process intentionally stopped outside trading hours. |
| **Webhook Ingest** | `MARKET_CLOSED` | **YES** | TradingView alert listener stopped outside trading hours. |
| **Trading Backend** | `HEALTHY` | **YES** | Backend application runs 24/7 to maintain database and dashboard API. |
| **Demo Streaming** | `HEALTHY` | **YES** | PnL dashboard server runs 24/7. |

> [!NOTE]
> Seeing `IB Gateway — MARKET_CLOSED` alongside `Trading Backend — HEALTHY` outside market hours is the **expected, normal system state**. No operator action is required.

---

## 4. Understanding the `/status` Telegram Command

At any time, an operator can type `/status` in the Telegram chat to request a complete system snapshot. Watchdog will reply with a structured report:

### Example `/status` Telegram Output

```text
🟢 SYSTEM STATUS — HEALTHY
━━━━━━━━━━━━━━━━━━━
TIME
10:15:30 EDT / 14:15:30 UTC / 19:45:30 IST

MARKET
🟢 OPEN
Session: 09:30–16:00 ET (Mon-Fri, America/New_York)

━━━━━━━━━━━━━━━━━━━
TRADING SERVICES
━━━━━━━━━━━━━━━━━━━
🟢 IB Gateway — HEALTHY (:4002) | Xvfb: RUNNING
  HTTP 200 -> Connected to TWS API port 4002
🟢 Trading Backend — HEALTHY (:8001)
  HTTP 200 -> OK
🟢 Webhook Ingest — HEALTHY (:8000)
  HTTP 200 -> OK

━━━━━━━━━━━━━━━━━━━
APPLICATION SERVICES
━━━━━━━━━━━━━━━━━━━
🟢 Demo Streaming — HEALTHY PID:99730
  HTTP 200 -> OK
🟢 PostgreSQL — HEALTHY PID:99712
  Connected to ibkr_trading (SELECT 1 -> OK)
🟢 Redis — HEALTHY PID:99714
  PONG -> OK
🟢 Watchdog — RUNNING PID:103881 CPU:0.2% RSS:45.2MB since 10:56

━━━━━━━━━━━━━━━━━━━
SYSTEM RESOURCES
━━━━━━━━━━━━━━━━━━━
🟢 CPU — 12.4% Load:0.45/4
🟢 RAM — 68.2% Used:5.4GB / 8.0GB Avail:2.6GB
🟢 Storage / — 42.1% Used:12.6GB / 30.0GB Free:17.4GB
🟢 Inodes / — 15.3% Used:153000 / 1000000

━━━━━━━━━━━━━━━━━━━
TRADING
━━━━━━━━━━━━━━━━━━━
Market: OPEN
Execution: ACTIVE (within session)
gateway: RUNNING
backend: RUNNING
webhook: RUNNING

━━━━━━━━━━━━━━━━━━━
ALERTS
━━━━━━━━━━━━━━━━━━━
🟢 No active infrastructure alerts

main-ec2 up 4d 12h | Linux 6.8.0-1015-aws
```

### System Status Values Explained

- **`SYSTEM STATUS — HEALTHY`**: All services are functioning and no safety gates are blocking trading during market hours.
- **`SYSTEM STATUS — MARKET_CLOSED`**: Market is closed; session-dependent services are intentionally stopped. Normal outside session.
- **`SYSTEM STATUS — DEGRADED`**: A resource warning (e.g. RAM $\ge 80\%$) or non-critical issue exists. Check the `ALERTS` section.
- **`SYSTEM STATUS — CRITICAL`**: A critical resource condition ($\ge 90\%$) or service failure exists. Read individual service alerts.

---

## 5. Safety-Gate Alerts Explained

Before allowing trade execution, the system evaluates four independent **Safety Gates**. All gates must be `SAFE` for trading to proceed.

```
          [ Safety Gate Checker ]
                     │
    ┌────────────────┼────────────────┬────────────────┐
    ▼                ▼                ▼                ▼
system_monitor   kill_switch       baskets        trading_mode
 (RAM/CPU/Disk)  (Account Arm)  (Order Errors)   (Port Check)
```

### The Four Safety Gates

1. **`system_monitor`**: Checks server health and hardware resource utilization.
   - `SAFE`: Hardware resources (RAM, CPU, Disk) are within normal operating bounds.
   - `UNSAFE`: Host RAM, CPU, or Disk reached critical threshold ($\ge 90\%$), or system monitor endpoint is unreachable.
2. **`kill_switch`**: Emergency account trading halt control.
   - `SAFE`: Kill switch is disarmed for all accounts.
   - `UNSAFE`: Kill switch is **ACTIVE** for one or more IBKR accounts. Trading is blocked until cleared by an operator.
3. **`baskets`**: Order basket risk management safety.
   - `SAFE`: No active `BASKET_CRITICAL` execution incidents.
   - `UNSAFE`: Order execution incident occurred (e.g., partial basket fill failure). Automatically pauses new orders.
4. **`trading_mode`**: Broker connection port validation.
   - `SAFE`: Gateway port matches paper trading (7497/4002) or approved live trading ports.
   - `UNKNOWN`: Unrecognized gateway port detected. Fails closed.

### How to Read Safety Gate Code Output in Messages

If Telegram displays:
```text
safety gates not all SAFE: {'system_monitor': 'UNSAFE', 'kill_switch': 'SAFE', 'baskets': 'SAFE', 'trading_mode': 'SAFE'}
```

**Translation**:
- `kill_switch = SAFE`: Kill switch is disarmed (normal).
- `baskets = SAFE`: Basket execution is healthy (normal).
- `trading_mode = SAFE`: Port configuration is valid (normal).
- `system_monitor = UNSAFE`: **This is the gate needing attention** (e.g. host RAM crossed 90%).

> [!WARNING]
> Never attempt to manually force trading or bypass a safety gate. Safety gates prevent financial loss and runaway execution.

---

## 6. Resource Alerts & Threshold Flapping

### Resource Alert Definitions

- **`RAM WARNING` / `CRITICAL`**: Server memory usage is elevated (Warning $\ge 80\%$, Critical $\ge 90\%$).
- **`CPU WARNING` / `CRITICAL`**: Server processing utilization is high (Warning $\ge 80\%$, Critical $\ge 90\%$).
- **`Storage WARNING` / `CRITICAL`**: Disk space is filling up (Warning $\ge 80\%$, Critical $\ge 90\%$).
- **`Inodes WARNING` / `CRITICAL`**: File system entry table is filling up.

### Why You May See "TRADING BLOCKED" Followed by "RECOVERED"

Resource utilization on servers naturally fluctuates up and down. If server RAM hovers around the 90.0% boundary:

```
Time 10:30:59 AM  ->  RAM 90.6%  ->  system_monitor = UNSAFE  ->  Alert: TRADING BLOCKED (RAM 90.6%)
Time 10:32:22 AM  ->  RAM 89.2%  ->  system_monitor = SAFE    ->  Alert: TRADING SAFETY CLEARED
Time 10:36:00 AM  ->  RAM 90.1%  ->  system_monitor = UNSAFE  ->  Alert: TRADING BLOCKED (RAM 90.1%)
```

**What this means for the operator**:
- This is threshold movement across the 90% boundary.
- **PostgreSQL and Trading Backend did NOT crash or restart.**
- Check the `SERVICE STATUS` field in Telegram: if `SERVICE STATUS` says `HEALTHY`, the service is working normally.

---

## 7. Component-by-Component Guide

| Component | Role | Expected State (Market Open) | Expected State (Market Closed) | Operator Notes |
| :--- | :--- | :--- | :--- | :--- |
| **PostgreSQL** | Primary Database | `HEALTHY` | `HEALTHY` | Stores orders, fills, and signals. Runs 24/7. |
| **Trading Backend** | Execution Engine | `HEALTHY` | `HEALTHY` | Runs 24/7. Evaluates signals and routes orders. |
| **IB Gateway** | TWS Broker Connection | `HEALTHY` | `MARKET_CLOSED` | Stopped outside session (09:30–16:00 ET). |
| **Webhook Ingest** | Alert Receiver | `HEALTHY` | `MARKET_CLOSED` | Receives TradingView webhooks. |
| **Watchdog** | Health Observer | `RUNNING` | `RUNNING` | Independent observer daemon. Runs 24/7. |
| **Demo Streaming** | Dashboard API | `HEALTHY` | `HEALTHY` | PnL dashboard server on port 8010. |
| **Redis** | In-Memory Cache | `HEALTHY` | `HEALTHY` | Used by demo dashboard for live streaming. |

---

## 8. "What Should I Do?" Operator Decision Matrix

| Observed Alert / Situation | System Meaning | Immediate Operator Action |
| :--- | :--- | :--- |
| Market closed + Gateway `MARKET_CLOSED` | Expected stop outside trading hours | **No action required.** Normal behavior. |
| Market closed + Webhook `MARKET_CLOSED` | Expected stop outside trading hours | **No action required.** Normal behavior. |
| Backend `HEALTHY` while market closed | Backend runs 24/7 for database/dashboard | **No action required.** Normal behavior. |
| All services `HEALTHY` | System operating normally | **No action required.** |
| Resource `WARNING` (e.g. RAM 82%) | Resource usage elevated but below critical | Issue `/status`; monitor if during trading session. |
| Resource alert clears (`RECOVERED`) | Metric returned to normal range | **No action required.** |
| `TRADING_BLOCKED` (Reason: RAM 90.6%) | Trading paused due to server RAM $\ge 90\%$ | Issue `/status`; notify technical lead if persistent. |
| `TRADING_BLOCKED` (Reason: kill switch) | Emergency kill switch is armed for account | Inspect Web Dashboard Settings; do **not** disarm without approval. |
| `TRADING_BLOCKED` (Reason: BASKET_CRITICAL) | Basket execution incident occurred | Inspect Web Dashboard Baskets page; follow basket clearance runbook. |
| Gateway `FAILED` during market **OPEN** | Broker connection dropped during trading | Issue `/status`; follow Level 3 escalation if unresolved after 2 mins. |
| Backend `FAILED` during market **OPEN** | Execution engine process down | Issue `/status`; escalate to technical lead immediately. |
| PostgreSQL `FAILED` | Database process unreachable | Escalate to technical lead immediately. |
| Watchdog `NOT FOUND` / Not Running | Monitoring daemon stopped | Escalate to technical lead immediately. |

---

## 9. "Do Not Do This" — Prohibited Actions

> [!CAUTION]
> **STRICT OPERATOR PROHIBITIONS**
> 1. **NEVER bypass a safety gate**: Do not modify configurations or force endpoints to make an alert turn green. Safety gates prevent unauthorized order execution.
> 2. **NEVER assume `TRADING_BLOCKED` means a service crashed**: Always check `SERVICE STATUS`. If `SERVICE STATUS` is `HEALTHY`, the service is working normally.
> 3. **NEVER assume `RECOVERED` means PostgreSQL or Backend restarted**: `RECOVERED` means the safety metric or condition returned to normal range.
> 4. **NEVER stop or restart random services**: Do not restart systemd services without understanding the exact root cause.
> 5. **NEVER ignore persistent `CRITICAL` resource alerts**: If host RAM or CPU remains $\ge 90\%$ continuously during market hours, escalate to technical support.
> 6. **NEVER disarm a Kill Switch without explicit authorization**: Kill switches are armed to protect trading capital.

---

## 10. Real Example Telegram Messages & Interpretations

### Example 1: PostgreSQL Healthy, Trading Safety Blocked
```text
🚨 WATCHDOG — TRADING BLOCKED
SERVICE: PostgreSQL
SERVICE STATUS: HEALTHY
TRADING STATUS: BLOCKED
DETAILS: Trading safety gate failed: system-monitor CRITICAL [RAM]: RAM usage critical (90.6%)
```
- **Plain-English Meaning**: PostgreSQL database is working normally (`SELECT 1 → OK`). Trading has been paused because host server memory reached 90.6%.
- **Operator Action**: No database action required. Monitor server resources via `/status`.

### Example 2: Trading Safety Cleared
```text
🟢 WATCHDOG — TRADING SAFETY CLEARED
SERVICE: Trading Safety Gate
TRADING STATUS: READY (subject to safety gates)
DETAILS: All trading safety gates passed (system_monitor, kill_switch, baskets, trading_mode SAFE).
```
- **Plain-English Meaning**: Server memory or safety condition returned to normal range. Trading readiness is restored.
- **Operator Action**: No action required.

### Example 3: Expected Market Closed Alert
```text
🟡 WATCHDOG — MARKET CLOSED
SERVICE: IB Gateway
SERVICE STATUS: MARKET_CLOSED
TRADING STATUS: MARKET CLOSED
DETAILS: IB Gateway is intentionally stopped outside the US trading session (weekdays 09:30–16:00 ET).
```
- **Plain-English Meaning**: The US trading session is closed. IB Gateway was stopped as scheduled.
- **Operator Action**: No action required. This is normal.

### Example 4: Active Kill Switch Alert
```text
🚨 WATCHDOG — TRADING BLOCKED
SERVICE: Trading Safety Gate
TRADING STATUS: BLOCKED
DETAILS: kill switch ACTIVE for: DU123456
```
- **Plain-English Meaning**: An emergency kill switch is armed for account DU123456. New trades will not be executed.
- **Operator Action**: Confirm with trading desk lead before taking any action.

---

## 11. Operator Escalation Procedure

```
    ┌─────────────────────────────────────────────────────────┐
    │                 Telegram Alert Received                 │
    └────────────────────────────┬────────────────────────────┘
                                 │
                     Issue /status in Telegram
                                 │
       ┌─────────────────────────┼─────────────────────────┐
       ▼                         ▼                         ▼
  [ Level 1 ]               [ Level 2 ]               [ Level 3 ]
    OBSERVE               INVESTIGATE                  ESCALATE
 (Market closed,         (Persistent DEGRADED,      (Persistent FAILED,
 temporary warning)     resource warning >5m)        Critical RAM >=90%)
```

- **Level 1 — Observe**:
  - Applies to: Market-closed alerts, brief resource warnings, alerts that immediately clear (`RECOVERED`).
  - Action: No technical action needed. Log observation if recurring.
- **Level 2 — Investigate**:
  - Applies to: `SYSTEM STATUS — DEGRADED` lasting $>5$ minutes, repeated resource warnings during active market hours.
  - Action: Send `/status` in Telegram. Note affected service and metrics.
- **Level 3 — Escalate Immediately**:
  - Applies to: `PostgreSQL FAILED`, `Trading Backend FAILED` during market hours, `IB Gateway FAILED` during market hours, Watchdog `NOT FOUND`, persistent `CRITICAL` RAM/CPU ($\ge 90\%$).
  - Action: Contact Lead Systems Engineer / Trading Desk Systems Lead with the `/status` output text.

---

## 12. Verification & Architecture Summary

| Property | Production Architecture Specification | Verified in Code |
| :--- | :--- | :---: |
| **Process Supervision** | Linux `systemd` (`ibgateway.service`, `trading-backend.service`, `webhook-ingest.service`, `demo-streaming.service`, `watchdog.service`) | ✅ |
| **Watchdog Role** | Independent observer daemon (never a supervisor; no restart authority) | ✅ |
| **Process Manager Status** | **Deprecated & Removed** (Not used in production) | ✅ |
| **Trading Window** | Weekdays 09:30–16:00 ET (America/New_York) | ✅ |
| **Fail-Closed Safety** | 4 Gates (`system_monitor`, `kill_switch`, `baskets`, `trading_mode`) | ✅ |
