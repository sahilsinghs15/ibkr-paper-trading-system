# Watchdog — monitoring, notification, deterministic recovery

**Verified from:** `backend/app/services/watchdog/*.py`, `backend/scripts/watchdog_main.py`, `deploy/systemd/*.service`, `backend/app/api/routes/health.py`.

Lightweight daemon (layer 3) — observes, classifies state transitions, sends Telegram, verifies recovery, escalates. Never a second process manager.

## Responsibility boundaries

| Layer | Owner | Protects |
|-------|-------|----------|
| systemd | OS | `process-manager.service`, `watchdog.service`, `demo-streaming.service` survival |
| process_manager.py | `process_manager` | Gateway (Xvfb+IBC), Backend `:8001`, Webhook `:8000` |
| systemd | OS | Demo Streaming `:8010` survival (not in process_manager) |
| watchdog | `watchdog` | Observe/notify/verify/escalate; no direct kill of Gateway/Backend |

No competing supervisors: watchdog never `kill -9` Gateway; process_manager owns restarts.

## Health — liveness vs readiness

| Service | Liveness | Readiness |
|---------|----------|-----------|
| Gateway `:4002` | TCP 127.0.0.1:4002 open | TCP + `ib_gateway.log` contains `Login has completed` (best-effort) |
| Backend `:8001` | `GET /health` 2xx | `GET /health/ready` (DB `SELECT 1` + TWS `is_connected`) + `GET /api/v1/system-monitor` if available |
| Webhook `:8000` | `GET /health` 2xx | same (DB `SELECT 1`) |
| Demo `:8010` | `GET /health` 2xx (redis flag) | redis `PING` |
| PostgreSQL | `SELECT 1` else TCP 5432 | same |
| Redis | `PING` else TCP 6379 | same |

New endpoints: `GET /health/live` and `GET /health/ready` on both FastAPI apps (existing `/health` remains liveness).

## State machine

```
UNKNOWN → STARTING → HEALTHY
HEALTHY → DEGRADED (readiness degraded) → FAILED
FAILED → RECOVERING → VERIFYING → RECOVERED → HEALTHY
VERIFYING (fail) → RECOVERING (retry) → MANUAL_INTERVENTION_REQUIRED (budget exhausted)
TRADING_BLOCKED (safety gate fails)
```

States: `UNKNOWN, STARTING, HEALTHY, DEGRADED, FAILED, RECOVERING, VERIFYING, RECOVERED, MANUAL_INTERVENTION_REQUIRED, TRADING_BLOCKED`.

Transitions via `state_machine.next_state()` (pure function). Budget: `WATCHDOG` sliding window `RECOVERY_MAX_ATTEMPTS=5` per `RECOVERY_WINDOW_SECONDS=600`.

## Telegram — detailed, structured

All alerts follow consistent ordering per spec (WATCHDOG → EVENT → SERVICE → STATUS → WHAT HAPPENED → ERROR DETAIL → WHERE/PID → IMPACT → RECOVERY → ATTEMPT → ACTION → TIME). Severity: `ℹ️ INFO` (START/RECOVERED/STOP), `⚠️ WARNING` (DEGRADED/UNHEALTHY/RECOVERY_STARTED), `🚨 CRITICAL` (FAILURE/RECOVERY_FAILED/MANUAL/TRADING_BLOCKED). `TELEGRAM_ALERT_LEVEL` filters (default WARNING).

`HealthResult` now carries structured diagnostics: `reason`, `host/port/pid/exit_code/signal`, `endpoint/url`, `dependency`, `underlying_error` (sanitized), `log_marker/log_excerpt` (≤400 chars, newest 3 lines), `what_happened/impact/trading_impact/operator_action`. Formatter never invents — uses `Not available` when unknown, never exposes `TELEGRAM_BOT_TOKEN/DATABASE_URL/password`.

Service-specific examples:

- **Gateway**: `tcp_refused` → `TCP 127.0.0.1:4002 refused`, `xvfb_missing` → `Xvfb :99 not running`, `login_marker_missing` → `Login has completed` not in `ib_gateway.log`; includes `LAST LOG DETAIL`.
- **Backend**: `tcp_refused` vs `http_failed_tcp_open` vs `readiness_failed` (`/health/ready` TWS/DB), PID via `psutil`; trading impact always `BLOCKED` on failure.
- **Webhook**: `readiness_failed_postgres` with `SELECT 1` error; impact notes `signal_jobs` durable.
- **Demo**: `redis_degraded` → explicit `TRADING IMPACT: None`.
- **Postgres**: `tcp_refused` vs `sql_timeout`/`sql_failed` with `SELECT 1`.
- **Redis**: `tcp_refused` vs `ping_failed`/`ping_timeout`.

Recovery lifecycle: `RECOVERY_STARTED` (attempt X/MAX + `EXPECTED VERIFICATION: process alive, /health/ready, dependencies`), `RECOVERED` (with `RECOVERY DURATION 23.4s`), `MANUAL_INTERVENTION_REQUIRED` (5/5 window 10m + root cause).

Config via env:

| Env | Default | Notes |
|-----|---------|-------|
| `TELEGRAM_BOT_TOKEN` | — | secret, never logged, redacted |
| `TELEGRAM_CHAT_ID` | — |  |
| `TELEGRAM_ENABLED` | `false` | must be true + both above to send |
| `TELEGRAM_ALERT_LEVEL` | `WARNING` | |
| `WATCHDOG_INTERVAL_SECONDS` | `10` | poll |
| `RECOVERY_MAX_ATTEMPTS` | `5` | budget |
| `RECOVERY_WINDOW_SECONDS` | `600` | |
| `WATCHDOG_HOST` | `main-ec2` | in message |

Events: `START, STOP, FAILURE, UNHEALTHY, RECOVERY_STARTED, RECOVERED, RECOVERY_FAILED, MANUAL_INTERVENTION_REQUIRED, TRADING_BLOCKED`. Deduplication: cooldown `NOTIFICATION_COOLDOWN_SECONDS=300` per `(service,event)`; queue bounded `100`, dropped counted; worker isolated from health loop.

## Safety gates — fail-closed

`SafetyGateChecker` is **fail-closed**: `UNKNOWN` → `UNSAFE` → `TRADING_BLOCKED`. Every gate must be `SAFE` for trading to be `READY`. Gates:

| Gate | Source | Safe condition | Unsafe/Unknown → |
|------|--------|----------------|------------------|
| `system_monitor` | `GET /api/v1/system-monitor` | `200` + `overall != CRITICAL` for gateway/postgres | `TRADING_BLOCKED` |
| `kill_switch` | `GET /api/v1/config/accounts` (`kill_switch_active`) | no account armed | `TRADING_BLOCKED` if any armed or API unreachable |
| `baskets` | `GET /api/v1/baskets/critical?ibkr_account=...` per account | `incidents == 0` for all accounts | `TRADING_BLOCKED` if any `>0` or cannot determine accounts |
| `trading_mode` | `gateway_port` ∈ {4002,7497,7496,4001} | recognized port | `UNKNOWN` → `TRADING_BLOCKED` |
| `recovery` | implicit via `baskets` + `system_monitor` | — | — |

`SafetyGateResult` returns `passed, failures, gates dict`. Trading-critical (`gateway, backend, postgres`) only declare `RECOVERED` after `safety.check()` passes; otherwise `TRADING_BLOCKED`. Unknown (API 500/timeout) **never** interpreted as safe.

## Recovery persistence — survives restart

`RecoveryBudgetStore` persists to `storage/state/watchdog_recovery.json` (configurable `recovery_state_path`). Format `{"gateway": ["2026-08-31T12:00:00+00:00"]}`. Atomic: `mkstemp` + `fsync` + `os.replace` + dir `fsync`. Corrupted JSON → `is_corrupted=True` → `is_exhausted=True` (fail-closed, budget treated as exhausted, log error, requires operator clear). Future timestamps `> now+60s` ignored + flagged corrupted. Window uses wall-clock `UTC` filtered `> cutoff` and `<= now+60`; monotonic not persisted, intra-process monotonic not needed — window is wall-clock with future-tolerance. Old attempts outside `recovery_window_seconds` (600s) expire.

## Notification priority — critical never lost

Queue total `100`, critical reserved `20`. Three buckets: `critical={FAILURE,MANUAL,TRADING_BLOCKED}`, `warning={UNHEALTHY,RECOVERY_STARTED,RECOVERY_FAILED}`, `info={START,STOP,RECOVERED}`. On full: critical evicts `info` then `warning` then oldest critical; warning evicts only `info`; info only evicts `info`. `dropped_count` logged. Worker drains `critical → warning → info`. Deduplication 300s per `(service,event)`, rate-limit `1/s`, async worker isolated from health loop.

## Systemd

```
deploy/systemd/process-manager.service → ExecStart .venv/bin/python scripts/process_manager.py (User=tradingapp, Restart=always, After=network/postgresql/redis)
deploy/systemd/demo-streaming.service   → ExecStart .venv/bin/python -m demo_streaming (User=tradingapp, Restart=always, After=network/postgresql/redis, MemoryMax=512M, independent — no After/Requires on process-manager)
deploy/systemd/watchdog.service         → ExecStart .venv/bin/python scripts/watchdog_main.py (User=tradingapp, Restart=always, After=network.target only, MemoryMax=256M, Requires=network.target only)
```

All `NoNewPrivileges=true PrivateTmp=true`. No `BindsTo/PartOf` coupling. Install: `sudo cp deploy/systemd/*.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now process-manager watchdog demo-streaming`. Rollback: `sudo systemctl disable --now watchdog` (trading unaffected).

## Logs

Watchdog: `storage/logs/{YYYY-MM-DD}/watchdog.log` + journald `watchdog` identifier. Never logs tokens.

## Security / Sanitization

- Bounded excerpts (400 chars, newest 3 lines only), `_sanitize` redacts `BOT_TOKEN/DATABASE_URL/password`.
- Never logs token, never dumps `.env`, never dumps full logs/stack.

## Tests

- `test_watchdog_state_machine.py` — 11 transitions
- `test_watchdog_notifications.py` — dedup, queue, telegram resilience
- `test_watchdog_recovery.py` — budget, safety, demo isolation
- `test_watchdog_audit.py` — independence, systemd units, resource
- `test_watchdog_telegram_detail.py` — 18 detail tests: gateway/backend/webhook/demo/postgres/redis reasons, recovery/start/manual/stop, ordering, severity, secrets, log bounds, unknown fields

Demo failure never marks trading blocked — verified by state isolation.
