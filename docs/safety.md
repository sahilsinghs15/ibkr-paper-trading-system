# Safety

**Verified from:** `backend/app/core/config.py`, `backend/app/api/routes/webhooks.py`, `backend/app/main.py`, `backend/app/instruments/` (paper STK→CFD override), `backend/app/accounts/router.py`.

## Paper vs live ports

| Port | Typical meaning | Enforced by app? |
|------|-----------------|------------------|
| `7497` | TWS paper (Settings default) | Default only |
| `4002` | Often IB Gateway paper | Not special-cased |
| `7496` | TWS live | **Not rejected** |
| `4001` | Gateway live | **Not rejected** |

`IBKR_PORT` is whatever the environment sets. Comments and naming say “paper”; code does **not** whitelist paper ports.

## Webhook executes

`POST /api/webhooks/tradingview` writes a capture file **and**, when `order_manager` is present, runs `process_signal_execution` (RMS → OMS → IBKR). It is **not** capture-only.

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
- No flatten-all endpoint
- No strategy-level pause API separate from DB `strategies.enabled` (not on Settings UI yet)

DB `enabled` flags on accounts / strategies / allocations affect routing when updated through the config API or out-of-band SQL.

## Dashboard exposure

The PnL + Settings UI on `:8010` has **no auth**. If you set `DEMO_STREAM_HOST=0.0.0.0`, restrict the AWS security group (TCP 8010) to your IP. Do not publish the trading app (`:8000`) on `0.0.0.0` for dashboard access. Config writes require the trading app running on `:8000` (local bind).

## Logging / secrets

- Do not commit `.env`, credentials, or webhook capture dumps.
- Application logger writes daily files to `storage/logs/trading-YYYY-MM-DD.log` (workspace root).
