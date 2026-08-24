# Safety

**Verified from:** `backend/app/core/config.py`, `backend/app/api/routes/webhooks.py`, `backend/app/main.py`, `backend/app/instruments/`, `backend/app/accounts/router.py`, `backend/app/services/kill_switch.py`.

## Paper vs live ports

| Port | Typical meaning | Enforced by app? |
|------|-----------------|------------------|
| `7497` | TWS paper (Settings default) | Default only |
| `4002` | Often IB Gateway paper | Not special-cased |
| `7496` | TWS live | **Not rejected** |
| `4001` | Gateway live | **Not rejected** |

`IBKR_PORT` is whatever the environment sets. Comments and naming say “paper”; code does **not** whitelist paper ports for ordinary trading.

**Paper-only behavior:** incomplete-leg basket retries and auto square-off are gated to ports `{7497, 4002}` via `paper_retry_ports_allowed()` in `oms/retry_policy.py`. Live ports never get those retries.

## Webhook behavior

`POST /api/webhooks/tradingview`:

1. Writes a capture file under `backend/data/tradingview_webhooks/`
2. Validates and enqueues a durable `signal_jobs` row
3. Returns **HTTP 202** with status **`accepted`**

Execution happens asynchronously in `ExecutionWorkerPool` workers — **not** inline in the HTTP handler (except legacy test path when pool and session_factory are both absent).

HTTP 202 `accepted` is **not** a fill confirmation. Check `signal_jobs.status` for outcome.

## Paper STK → CFD override

`paper_execute_stk_as_cfd` defaults to `True`. Requested STK may be executed as IBKR CFD for paper/demo; the raw / persisted signal instrument type can remain STK. Disable with `PAPER_EXECUTE_STK_AS_CFD=false` (tests often set this false in `conftest.py`).

When a symbol has no row in `instruments`, the trading app **best-effort** discovers IBKR CFD `conId` via `reqContractDetails` on the same TWS socket (before OPEN resolve and on startup `hydrate_live_pnl`). Unique matches are upserted; ambiguous or missing matches keep the demo no-conId path. Live PnL subscribes **CFD** market data with `conId` when available (no STK mark fallback).

Offline operator CLI (use client id **99** so it does not clash with uvicorn):

```bash
cd /home/tradingapp/app/backend
.venv/bin/python scripts/instrument_master/discover_cfd.py --port 4002 XLE XOP SIL GDX
```

## Operator controls

- **Settings page** (`:8010/settings`): edit `accounts.total_margin`, `accounts.enabled`, `allocations.alloc_pct` / `enabled` / `max_open_positions`, and `per_symbol_limits` via proxied `/api/v1/config/*`.
- **Kill switch API** (trading app `:8000`): `POST /api/v1/config/accounts/{id}/square-off` (202), `GET .../kill-switch`, `POST .../kill-switch/clear`. See [`backend-kill-switch.md`](backend-kill-switch.md).
- Kill switch **stays armed** after flatten completes until explicit clear — completing flatten is not the same as disarming.
- No strategy-level pause API separate from DB `strategies.enabled` (not on Settings UI yet)

DB `enabled` flags on accounts / strategies / allocations affect routing when updated through the config API or out-of-band SQL.

## Dashboard exposure

The PnL + Settings UI on `:8010` has **no auth**. If you set `DEMO_STREAM_HOST=0.0.0.0`, restrict the AWS security group (TCP 8010) to your IP. Do not publish the trading app (`:8000`) on `0.0.0.0` for dashboard access. Config writes require the trading app running on `:8000` (local bind).

## Logging / secrets

- Do not commit `.env`, credentials, or webhook capture dumps.
- Application logger writes daily files to `storage/logs/trading-YYYY-MM-DD.log` (workspace root).

## Disk retention

Every webhook writes a raw JSON capture to `backend/data/tradingview_webhooks/` and nothing removes them, so the directory grows for the life of the host. Captures are a debugging aid — the durable record of a signal is Postgres (`signals`, `signal_jobs`), so pruning is safe.

```bash
.venv/bin/python scripts/prune_webhook_captures.py --days 14          # dry run
.venv/bin/python scripts/prune_webhook_captures.py --days 14 --apply
```

Suggested retention is 14 days; run it from cron on long-lived hosts. `backend/data/` is gitignored.

## Submit pacing

Production uses `OrderSubmitPacer(min_interval_sec=0.2)` on the IBKR adapter — all `placeOrder` calls including kill-switch flatten share this **process-global** minimum interval. It is not 50 msg/sec, not per account, not per Gateway (there is only one socket), and it does not cover `reqMktData`. `IBKRExecutionScheduler` priority queues are **not** wired in production.

`main.py` may log that the adapter will auto-reconnect if the startup handshake fails. **That reconnect is not implemented** (`IBKRExecutionAdapter.submit_order` raises `ConnectionError`; `on_connection_closed` marks in-memory working orders `ERROR` without querying IB). Treat a dropped socket as operator action + restart until reconnect is built. See [`backend-multi-gateway.md`](backend-multi-gateway.md).
