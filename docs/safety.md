# Safety

**Verified from:** `backend/app/core/config.py`, `backend/app/api/routes/webhooks.py`, `backend/app/main.py`, `backend/app/instruments/`, `backend/app/accounts/router.py`, `backend/app/services/kill_switch.py`.

## Live Gateway port

| Port | Typical meaning | Enforced by app? |
|------|-----------------|------------------|
| `4001` | Live Gateway (Settings default) | **The production port** |
| `7497` | TWS paper | Not special-cased |
| `4002` | Gateway paper | Not special-cased |
| `7496` | TWS live | **Not rejected** |

`IBKR_PORT` defaults to **4001**. Watchdog probes 4001. Remainder-retry is allowed on 4001 after M9/M14 identity fixes (`paper_retry_ports_allowed` includes 4001).

## Webhook behavior

`POST /api/webhooks/tradingview`:

1. Authenticates `X-Webhook-Secret` (fail-closed if auth is enabled and the secret is unset)
2. Validates and enqueues a durable `signal_jobs` row (`capture_data` is the payload authority)
3. Returns **HTTP 202** with status **`accepted`**

Execution happens asynchronously in `ExecutionWorkerPool` workers — **not** inline in the HTTP handler.

HTTP 202 `accepted` is **not** a fill confirmation. Check `signal_jobs.status` for outcome.

## Model Blue STK → CFD

`execute_stk_as_cfd` defaults to `True`. TradingView sends STK; submit maps to IBKR CFD. The raw / persisted signal instrument type stays STK; executed `secType` is logged at resolve time. Disable with `EXECUTE_STK_AS_CFD=false` (legacy env `PAPER_EXECUTE_STK_AS_CFD` is still accepted). This is production Model Blue, not a paper/demo map.

When a symbol has no row in `instruments`, the trading app **best-effort** discovers IBKR CFD `conId` via `reqContractDetails` on the same TWS socket (before OPEN resolve and on startup `hydrate_live_pnl`). Unique matches are upserted; ambiguous or missing matches keep the no-conId path. Live PnL subscribes **CFD** market data with `conId` when available (no STK mark fallback).

Offline operator CLI (use client id **99** so it does not clash with uvicorn):

```bash
cd /home/tradingapp/app/backend
.venv/bin/python scripts/instrument_master/discover_cfd.py --port 4001 XLE XOP SIL GDX
```

## Operator controls

- **Settings page** (`:8010/settings`): edit **Trading capital** (`accounts.total_margin` — operator-entered market-value budget, not broker margin), `accounts.enabled`, `allocations.alloc_pct` / `pair_max_allocation_pct` / `enabled` / `max_open_positions`, and `per_symbol_limits` via proxied `/api/v1/config/*`. RMS check 101 bounds **gross market value** of open pairs only (`MARKET_VALUE_CHECK_ENABLED=false` until flipped); nothing watches actual broker margin utilisation except check 1 (also shadow by default). Pair sizing ships with `PAIR_RATIO_TOLERANCE=0.5` and `PAIR_MIN_DEPLOYMENT_PCT=0`.
- **Kill switch API** (trading app `:8001`): `POST /api/v1/config/accounts/{id}/square-off` (202), `GET .../kill-switch`, `POST .../kill-switch/clear`. IBKR leftover flatten (sidecar, client id 99): [`backend-kill-switch.md`](backend-kill-switch.md).
- Kill switch **stays armed** after flatten completes until explicit clear — completing flatten is not the same as disarming.
- No strategy-level pause API separate from DB `strategies.enabled` (not on Settings UI yet)

DB `enabled` flags on accounts / strategies / allocations affect routing when updated through the config API or out-of-band SQL.

## Dashboard exposure

The PnL + Settings UI on `:8010` has **no auth**. If you set `DEMO_STREAM_HOST=0.0.0.0`, restrict the AWS security group (TCP 8010) to your IP. Do not publish the trading app (`:8001`) on `0.0.0.0`. Keep ngrok on webhook ingest `:8000` only. Config writes require the trading app running on `:8001` (local bind).

## Logging / secrets

- Do not commit `.env`, credentials, or webhook capture dumps.
- Application logger writes daily files to `storage/logs/{YYYY-MM-DD}/trading.log` (workspace root).

## Disk retention

Webhook payload authority is Postgres `signal_jobs.capture_data`. JSON `webhook_*.json` files are no longer written. The TEMPORARY CSV `incoming_signals.csv` may still grow under `backend/data/tradingview_webhooks/`.

```bash
.venv/bin/python scripts/prune_webhook_captures.py --days 14          # dry run
.venv/bin/python scripts/prune_webhook_captures.py --days 14 --apply
```

Suggested retention is 14 days; run it from cron on long-lived hosts. `backend/data/` is gitignored.

## Submit pacing

Production uses `GatewayRateLimiter` on the IBKR adapter — token bucket ~30 msg/sec (configurable via Settings), P0 emergency reserve for flatten, wait+timeout, Error 100 cooldown. Shared by **all** accounts on the **one** socket. Also paces `cancelOrder` and counts `reqMktData`. Do not run `uvicorn --workers N` against the same Gateway (each worker gets its own limiter).

**IBKR disconnects the socket if the client exceeds ~50 messages/sec.** The limiter's ~30 ceiling is headroom under that hard cap. Breaching it is not a slowdown — it drops live trading.

Limiter **priority does not isolate probes from orders.** In `_try_consume_locked` only P0 is special-cased. P1 (orders) and P4 (`PRIORITY_DIAGNOSTIC`) both require one global **and** one normal token. Tagging a what-if P4 is metrics-only; it can consume the token a live order needed. The margin scanner therefore uses its own token bucket plus a worker-pool-busy skip.

### What-if / margin probes

`IBKRExecutionAdapter.probe_margin` sends `placeOrder` with `whatIf=True`. That **burns a real `orderId`** from `_get_next_tws_order_id()`. A dropped `whatIf` flag is a live order — the adapter asserts the flag before send. Probe ids live in `_whatif_pending`, not `_orders_by_tws_id`, and `cancelOrder` runs in `finally`.

IBKR may return `inf` / Double.MAX_VALUE for `initMarginChange`. That is **unknown**: skip / reject (`MARGIN_PROBE_UNKNOWN`), never guess. `MARGIN_WHATIF_ENABLED=false` (default) never calls `placeOrder`.

CLOSE and emergency flatten skip Gate A and check 1 — blocking a close because margin is tight is backwards.

**First full scan (operator):** set `MARGIN_SCAN_ENABLED=true` and `MARGIN_WHATIF_ENABLED=true`, then uncheck **Margin check enabled** (shadow) if rates look wrong. Confirm CFD rates land in the 10–20% range (not ~50%) before leaving check 1 on. Default `check_enabled` is true; flip via `PATCH /api/v1/config/margin`.

`main.py` may log that the adapter will auto-reconnect if the startup handshake fails. **That reconnect is not implemented** (`IBKRExecutionAdapter.submit_order` raises `ConnectionError`; `on_connection_closed` marks in-memory working orders `ERROR` without querying IB). Treat a dropped socket as operator action + restart until reconnect is built. See [`backend-multi-gateway.md`](backend-multi-gateway.md).
