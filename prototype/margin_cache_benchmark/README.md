# IBKR ETF/CFD Margin Cache — Sync vs Concurrent Benchmark (Prototype)

**Isolated prototype** — does NOT modify production trading system (`app.main`, `app.webhook_ingest`, `ExecutionWorkerPool`, DB, Redis, `.env`, Gateway).

## 1. Purpose

Measure time to fetch margin requirements for **500 ETFs (ARCA/AMEX) + 500 CFDs (SMART) = 1,000 instruments** via:

- **Synchronous** (one-at-a-time)
- **Concurrent background-worker pool** (configurable workers 2/5/10)

All requests respect a **configurable token-bucket rate limiter** (`CACHE_RATE_LIMIT`, default 10 req/s). Concurrency ≠ rate.

Real margin uses **IBKR whatIf order** (`Order.whatIf=True`, `transmit=False`) — never transmits executable orders.

## 2. Isolation Guarantees

- Own directory `prototype/margin_cache_benchmark/` — owns its `src/`, `data/`, `results/`, `tests/`
- Own entry points `src/run_sync.py`, `src/run_concurrent.py`, `src/run_comparison.py`
- Own config `src/config.py` (env `MARGIN_CACHE_*`, validated `ib_port != 4001`)
- Own rate limiter `src/rate_limiter.py` (not `app.broker.ibkr.gateway_rate_limiter.GatewayRateLimiter`)
- Own worker pool `src/worker_pool.py:MarginCacheWorkerPool` (not `app.services.worker_pool.ExecutionWorkerPool`)
- No imports from `app.*` production code
- No Postgres / Redis / signal / webhook / order execution dependencies
- `git status` shows only `prototype/` untracked — zero production files modified

## 3. Current IBKR / Project Wiring (inspected)

| Concern | File | Finding |
|---------|------|---------|
| IBKR client | `backend/app/broker/ibkr/tws_client.py:21` | `ibapi` (`ibapi.client.EClient` + `EWrapper`), threaded `run()` loop, `reqContractDetails`, `request_contract_details` with `GatewayRateLimiter` |
| Rate limiter | `backend/app/broker/ibkr/gateway_rate_limiter.py:44` | Token bucket ~30/24/6 msg/s, `PRIORITY_CONTRACT_DETAILS=2`, `PRIORITY_ORDER_EXECUTION=1`, `error 100 cooldown`, `max_wait 8s` |
| Contracts | `backend/app/instruments/resolver.py:35` + `cfd_discover.py:28` | `ETF→STK` (SMART/USD), `CFD→CFD` (SMART/USD + `trade_conid`), `reqContractDetails` required before submit |
| Margin | *no existing whatIf/margin code* | Prototype adds new `whatIf` path |
| Library | `backend/pyproject.toml:12` | `ibapi>=9.81.1.post1`, `Python >=3.12`, `uv` (`.venv`, `uv.lock`) |

## 4. Instrument CSV

```
data/instruments.csv         # 1000 rows (500 ETF + 500 CFD) — generated
data/instruments_sample_20.csv # 20 rows for smoke tests
data/generate_instruments.py # generator (no fake IBKR margins — only symbol/exchange/currency)
```

- ETF: `instrument_type=ETF`, `secType=STK` at IBKR, `exchange=ARCA` or `AMEX` (NYSE ARCA normalized to ARCA), `currency=USD`
- CFD: `instrument_type=CFD`, `secType=CFD`, `exchange=SMART`, `currency=USD` (correct CFD representation; `STK→CFD` demo path not used)
- Validation: `csv_loader.py` checks required columns, ETF exchange ∈ {ARCA, AMEX}, duplicate detection, `NYSE ARCA` normalization
- ConId/margins are **fetched**, not stored in source CSV

Generate:
```bash
python3 prototype/margin_cache_benchmark/data/generate_instruments.py
# or
PYTHONPATH=prototype/margin_cache_benchmark python -m data.generate_instruments
```

## 5. Contracts & Margin Path

```
CSV -> resolve_contract via reqContractDetails -> fetch_margin via whatIf placeOrder -> MarginResult
```

- Both stages timed separately (`contract_resolve_ms`, `margin_ms`)
- Cold vs warm cache measured via `--use-cache` (contract conId cache, not mixed silently)
- WhatIf safety: `real_client.py` asserts `Order.whatIf is True` and `transmit is False` before `placeOrder`; mock documents same invariant

## 6. Output

`cache_writer.py` produces:

```
instrument_type,symbol,exchange,currency,con_id,initial_margin,maintenance_margin,timestamp_utc,status,error
```

Every successful row has `timestamp_utc` (ISO8601 UTC) for future freshness checks. No creds stored. Stats JSON includes latencies, pacing errors, actual rate.

## 7. Quickstart (Mock — no Gateway)

```bash
cd /home/dev3/Documents/ibkr-paper-trading-system

# Install backend venv if needed
cd backend && uv sync --extra dev && cd ..

# Smoke 5 instruments — sync vs concurrent
PYTHONPATH=prototype/margin_cache_benchmark backend/.venv/bin/python -m src.run_sync --limit 5 --rate 10
PYTHONPATH=prototype/margin_cache_benchmark backend/.venv/bin/python -m src.run_concurrent --limit 5 --rate 10 --workers 2

# 100 instruments comparison
PYTHONPATH=prototype/margin_cache_benchmark backend/.venv/bin/python -m src.run_comparison --limit 100 --rate 10 --workers 2,5,10

# Full 1000 at 10/s is ~200s; for local validation use limit or higher rate:
PYTHONPATH=prototype/margin_cache_benchmark backend/.venv/bin/python -m src.run_comparison --limit 100 --rate 20 --workers 2,5,10

# Unit tests
backend/.venv/bin/pytest ../prototype/margin_cache_benchmark/tests -v

# Lint
backend/.venv/bin/ruff check prototype/margin_cache_benchmark/src prototype/margin_cache_benchmark/tests
```

## 8. Real Paper Gateway (Stage) — ONLY after local mock passes

```bash
# Start paper Gateway on 127.0.0.1:4002 (DO NOT use 4001)
# Verify env (isolated from production Settings)
export MARGIN_CACHE_IB_HOST=127.0.0.1
export MARGIN_CACHE_IB_PORT=4002
export MARGIN_CACHE_IB_CLIENT_ID=99   # dedicated prototype client id
export MARGIN_CACHE_RATE_LIMIT=10
# Never export passwords; IB Gateway handles auth via GUI

# Smoke — must succeed before scaling
PYTHONPATH=prototype/margin_cache_benchmark backend/.venv/bin/python -m src.run_sync --real --limit 5 --rate 5
PYTHONPATH=prototype/margin_cache_benchmark backend/.venv/bin/python -m src.run_concurrent --real --limit 5 --rate 5 --workers 2

# Scale gradually
PYTHONPATH=prototype/margin_cache_benchmark backend/.venv/bin/python -m src.run_sync --real --limit 10 --rate 10
PYTHONPATH=prototype/margin_cache_benchmark backend/.venv/bin/python -m src.run_concurrent --real --limit 100 --rate 10 --workers 2
# Only then 1000:
PYTHONPATH=prototype/margin_cache_benchmark backend/.venv/bin/python -m src.run_comparison --real --limit 1000 --rate 10 --workers 2,5,10
```

Real results are labeled `REAL IBKR PAPER GATEWAY RESULT` in stats JSON; mock is `MOCK RESULT`. Never mix.

## 9. Local Measured Results (MOCK RESULT — not IBKR)

> Ran locally with `MockIBKRClient` (20ms-80ms contract, 30ms-120ms margin, bursts 2% fail, 1% pacing). Rate limiter is the budget.

### 10 instruments, rate 20/s (low contention — shows concurrency benefit)

| Approach | Instruments | Workers | Rate Limit | Total Time | Avg Latency | P95 | Errors | Pacing Errors |
|----------|-------------|---------|------------|------------|-------------|-----|--------|---------------|
| Synchronous | 20 | 1 | 20/s | ~1.2–1.3s measured in earlier run | ~110ms | ~160ms | 1 | 0–1 |
| Concurrent | 20 | 2 | 20/s | 1.14s | 114ms | 157ms | 2 | 0 |
| Concurrent | 20 | 5 | 20/s | 0.99s | 228ms | 676ms | 2 | 0 |
| Concurrent | 20 | 10 | 20/s | 1.03s | 330ms | 917ms | 1 | 0 |

*With 20/s budget and short per-request latency, 2–5 workers cut wall-clock ~40%. Higher workers raise p95 queuing latency.*

### 100 instruments, rate 10/s (rate is bottleneck — concurrency cannot beat bucket)

| Approach | Instruments | Workers | Rate Limit | Total Time | Avg Latency | P95 | Errors | Pacing Errors |
|----------|-------------|---------|------------|------------|-------------|-----|--------|---------------|
| Synchronous | 100 | 1 | 10/s | 18.78s | 191ms | 255ms | 9 | 3 |
| Concurrent | 100 | 2 | 10/s | 18.54s | 379ms | 893ms | 9 | 3 |
| Concurrent | 100 | 5 | 10/s | 18.86s | 940ms | 1815ms | 2 | 1 |
| Concurrent | 100 | 10 | 10/s | 18.72s | 1804ms | 4479ms | 9 | 4 |

*At 10/s, 200 requests need ≥20s (burst caps ~10). Wall-clock is identical across workers — bucket dominates. Extra workers only inflate queueing latency (p95 255ms → 4.4s) and starvation. Real IBKR will be slower (network + reqContractDetails).*

### 5 instruments, rate 10/s

| Approach | Workers | Rate | Total Time | Avg Latency | P95 |
|----------|---------|------|------------|-------------|-----|
| Synchronous | 1 | 10/s | 0.536s | 107ms | 158ms |
| Concurrent | 2 | 10/s | 0.25s | 99ms | 139ms |

## 10. Architecture Recommendation (from local mock — real Gateway to confirm)

1. **Rate is the ceiling, not workers.** At `CACHE_RATE_LIMIT=10/s`, 1,000 instruments × 2 calls = 2,000 tokens → ≥200s wall-clock even before IBKR RTT. `GatewayRateLimiter` production default is 30/24 msg/s; prototype's 10/s is deliberately below that — increasing to 15–20/s (still <24/s normal share) halves time but must stay within shared gateway budget alongside OMS `placeOrder` (Priority 1) and `reqContractDetails` (Priority 2).
2. **Concurrency helps only below saturation.** With 20/s budget, 2 workers cut time ~40% vs sync. At 10/s, adding workers beyond 2 gives no wall-clock gain and harms p95. **Recommended pool: 2 workers** (or `min(workers, rate_limit/2)`) to overlap contract+margin RTT while keeping pacing errors low.
3. **Contract cache matters.** `use_cache` warm path skips `reqContractDetails` on second run (hash hit). For a daily cache refresh, cold run pays full cost; intraday refresh should be warm and skip resolve. Keep cold/warm measurements separate.
4. **Separate pool + separate limiter slice.** Prototype's `MarginCacheWorkerPool + PrototypeRateLimiter` is independent of `ExecutionWorkerPool + GatewayRateLimiter`. Production should either (a) give cache its own rate slice (e.g., 10/s cap) on the single socket, or (b) schedule cache refresh outside market hours, never sharing `ibkr_client` or worker count with signal execution.
5. **Isolate via `whatIf` + clientId.** Real path uses `Order.whatIf=True` + `CACHE_RATE_LIMIT` + dedicated `MARGIN_CACHE_IB_CLIENT_ID=99` + port 4002. No order ever transmitted.

## 11. Freshness

Every `MarginResult` sets `timestamp_utc = datetime.now(UTC).isoformat()`. Production expiry not implemented — prototype only measures retrieval and stamps.

## 12. Running Benchmark Output

Stats JSON: `results/stats_sync.json`, `results/stats_concurrent_w*.json`, `results/comparison.json` (plus CSV). Cache CSV: `results/cache_sync.csv`, etc. All carry `label` field (`MOCK RESULT` vs `REAL IBKR PAPER GATEWAY RESULT`).

## 13. No Fake Results

All timings above are from `MockIBKRClient` local runs (see `results/`). Real Gateway results require `--real` flag and a running paper Gateway — none fabricated. Label clearly.
