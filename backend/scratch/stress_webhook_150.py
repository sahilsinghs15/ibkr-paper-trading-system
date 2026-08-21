"""150-Signal Webhook Burst / Concurrency Stress Test Script.

Performs a stress test against the TradingView webhook using valid model_blue signal payloads.
Tests HTTP webhook ingestion, PostgreSQL signal_jobs queueing, and background worker pool execution.
"""

import asyncio
import json
import logging
import math
import os
import sys
import time
from typing import Any
import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Import database session helper
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.db.session import create_engine_from_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("stress_webhook_150")

DEFAULT_TARGET_URL = "http://127.0.0.1:8000/api/webhooks/tradingview"


def percentile(data: list[float], pct: float) -> float:
    """Calculate percentile using linear interpolation."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * (pct / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_data[int(k)]
    d0 = sorted_data[int(f)] * (c - k)
    d1 = sorted_data[int(c)] * (k - f)
    return d0 + d1


def generate_stress_payloads(count: int = 150, prefix: str = "MBG-STRESS150") -> list[dict[str, Any]]:
    """Generate `count` distinct valid model_blue signal payloads."""
    payloads = []
    timestamp_str = time.strftime("%Y%m%dT%H%M%S")
    pairs = [("NOBL", "SPY"), ("EWP", "EWU"), ("XLF", "XLI"), ("FDN", "XLF"), ("AAPL", "MSFT")]
    for i in range(count):
        sym_a, sym_b = pairs[i % len(pairs)]
        trade_id = f"{prefix}-{timestamp_str}-{i:04d}"
        payloads.append(
            {
                "strategy": "model_blue",
                "action": "OPEN",
                "trade_id": trade_id,
                "direction": 1 if (i % 2 == 0) else -1,
                "market": "SMART",
                "buckets": [
                    {
                        "underlying": sym_a,
                        "legs": [
                            {
                                "instrument_type": "STK",
                                "side": "BUY",
                                "weight": 1.0,
                                "price": f"{50.00 + (i % 10):.2f}",
                            }
                        ],
                    },
                    {
                        "underlying": sym_b,
                        "legs": [
                            {
                                "instrument_type": "STK",
                                "side": "SELL",
                                "weight": 1.0,
                                "price": f"{100.00 + (i % 10):.2f}",
                            }
                        ],
                    },
                ],
            }
        )
    return payloads


async def send_single_webhook(
    client: httpx.AsyncClient, target_url: str, payload: dict[str, Any], index: int
) -> dict[str, Any]:
    """Send single webhook request and record precise latency and response."""
    t_send = time.monotonic()
    t_wall_send = time.time()
    try:
        response = await client.post(target_url, json=payload)
        t_recv = time.monotonic()
        elapsed_ms = (t_recv - t_send) * 1000.0
        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text}

        return {
            "index": index,
            "trade_id": payload["trade_id"],
            "send_wall_time": t_wall_send,
            "send_mono_time": t_send,
            "recv_mono_time": t_recv,
            "status_code": response.status_code,
            "latency_ms": elapsed_ms,
            "body": body,
            "error": None,
        }
    except Exception as exc:
        t_recv = time.monotonic()
        elapsed_ms = (t_recv - t_send) * 1000.0
        return {
            "index": index,
            "trade_id": payload["trade_id"],
            "send_wall_time": t_wall_send,
            "send_mono_time": t_send,
            "recv_mono_time": t_recv,
            "status_code": 0,
            "latency_ms": elapsed_ms,
            "body": None,
            "error": str(exc),
        }


async def audit_database_persistence(trade_ids: list[str]) -> dict[str, Any]:
    """Query PostgreSQL signal_jobs table to audit persistence and lifecycle statuses."""
    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        result = await session.execute(
            text(
                "SELECT job_id, signal_id, trade_id, status, last_error, attempt_count, worker_id, "
                "received_at, queued_at, lease_expires_at, processing_started_at, completed_at "
                "FROM signal_jobs WHERE trade_id = ANY(:tids)"
            ),
            {"tids": trade_ids},
        )
        rows = result.mappings().all()

    await engine.dispose()

    persisted_by_trade_id = {row["trade_id"]: dict(row) for row in rows}
    status_counts: dict[str, int] = {}
    worker_counts: dict[str, int] = {}

    for row in rows:
        st = row["status"]
        status_counts[st] = status_counts.get(st, 0) + 1
        w_id = row["worker_id"] or "unclaimed"
        worker_counts[w_id] = worker_counts.get(w_id, 0) + 1

    missing_trade_ids = [tid for tid in trade_ids if tid not in persisted_by_trade_id]

    return {
        "total_expected": len(trade_ids),
        "total_persisted": len(rows),
        "status_counts": status_counts,
        "worker_counts": worker_counts,
        "missing_trade_ids": missing_trade_ids,
        "records": persisted_by_trade_id,
    }


async def run_burst_stress_test(target_url: str, count: int = 150) -> dict[str, Any]:
    """Execute concurrent 150-signal burst and compile comprehensive stress test metrics."""
    logger.info("Generating %d stress test model_blue payloads...", count)
    prefix = f"MBG-STRESS{count}"
    payloads = generate_stress_payloads(count, prefix=prefix)
    trade_ids = [p["trade_id"] for p in payloads]

    limits = httpx.Limits(max_keepalive_connections=200, max_connections=500)
    timeout = httpx.Timeout(30.0, connect=10.0)

    logger.info("Launching concurrent burst of %d requests to %s...", count, target_url)
    t_start_burst = time.monotonic()
    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        tasks = [send_single_webhook(client, target_url, payloads[i], i) for i in range(count)]
        results = await asyncio.gather(*tasks)
    t_end_burst = time.monotonic()

    total_burst_duration = t_end_burst - t_start_burst

    status_codes = [r["status_code"] for r in results]
    latencies = [r["latency_ms"] for r in results]
    errors = [r for r in results if r["error"] is not None]

    c_2xx = sum(1 for sc in status_codes if 200 <= sc < 300)
    c_4xx = sum(1 for sc in status_codes if 400 <= sc < 500)
    c_5xx = sum(1 for sc in status_codes if 500 <= sc < 600)
    c_timeouts = sum(1 for r in results if r["error"] and "timeout" in r["error"].lower())
    c_conn_fail = len(errors) - c_timeouts

    lat_min = float(min(latencies))
    lat_p50 = float(percentile(latencies, 50))
    lat_p90 = float(percentile(latencies, 90))
    lat_p95 = float(percentile(latencies, 95))
    lat_p99 = float(percentile(latencies, 99))
    lat_max = float(max(latencies))
    lat_avg = float(sum(latencies) / len(latencies))

    logger.info("Burst completed in %.3fs. Waiting 3.0s for worker processing before DB audit...", total_burst_duration)
    await asyncio.sleep(3.0)
    db_audit = await audit_database_persistence(trade_ids)

    return {
        "target_url": target_url,
        "count": count,
        "total_burst_duration_sec": total_burst_duration,
        "throughput_rps": count / max(0.001, total_burst_duration),
        "http_summary": {
            "submitted": count,
            "acked_2xx": c_2xx,
            "client_4xx": c_4xx,
            "server_5xx": c_5xx,
            "timeouts": c_timeouts,
            "connection_failures": c_conn_fail,
        },
        "latency_ms": {
            "min": lat_min,
            "p50": lat_p50,
            "p90": lat_p90,
            "p95": lat_p95,
            "p99": lat_p99,
            "max": lat_max,
            "avg": lat_avg,
        },
        "db_audit": db_audit,
    }


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TARGET_URL
    count_val = int(sys.argv[2]) if len(sys.argv) > 2 else 150
    report = asyncio.run(run_burst_stress_test(target, count_val))
    print(json.dumps(report, indent=2, default=str))
