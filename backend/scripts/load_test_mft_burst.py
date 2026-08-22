"""Burst load test for TradingView webhook ingestion.

Sends N concurrent model_blue OPEN payloads at the running trading app and reports
HTTP ack rate plus latency percentiles. With --audit it also queries signal_jobs so
you can see how the durable queue and worker pool drained the burst.

Requires the trading app on --url. --audit additionally needs DATABASE_URL to point
at the same Postgres the app uses.

    .venv/bin/python scripts/load_test_mft_burst.py --count 150 --audit
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
import time
from typing import Any

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("load_test_mft_burst")

DEFAULT_TARGET_URL = "http://127.0.0.1:8000/api/webhooks/tradingview"
DEFAULT_PAIRS = [
    ("NOBL", "SPY"),
    ("EWP", "EWU"),
    ("XLF", "XLI"),
    ("FDN", "XLK"),
    ("AAPL", "MSFT"),
]
# HTTP ingestion must stay fast because execution is asynchronous; a slow ack means
# the webhook is doing work that belongs to the worker pool.
ACK_LATENCY_BUDGET_MS = 2000.0


def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile (avoids a numpy dependency)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * (pct / 100.0)
    low = math.floor(k)
    high = math.ceil(k)
    if low == high:
        return ordered[int(k)]
    return ordered[low] * (high - k) + ordered[high] * (k - low)


def generate_payloads(count: int, prefix: str) -> list[dict[str, Any]]:
    """Build `count` distinct valid model_blue OPEN payloads."""
    stamp = time.strftime("%Y%m%dT%H%M%S")
    payloads: list[dict[str, Any]] = []
    for i in range(count):
        sym_a, sym_b = DEFAULT_PAIRS[i % len(DEFAULT_PAIRS)]
        payloads.append(
            {
                "strategy": "model_blue",
                "action": "OPEN",
                "trade_id": f"{prefix}-{stamp}-{i:04d}",
                # Parser requires an integer +1 / -1, not "LONG"/"SHORT".
                "direction": 1 if i % 2 == 0 else -1,
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
                                "weight": -1.0,
                                "price": f"{100.00 + (i % 10):.2f}",
                            }
                        ],
                    },
                ],
            }
        )
    return payloads


async def send_one(
    client: httpx.AsyncClient, url: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """POST a single webhook and record status plus latency."""
    started = time.monotonic()
    try:
        response = await client.post(url, json=payload)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        try:
            body = response.json()
        except ValueError:
            body = {"raw": response.text}
        return {
            "trade_id": payload["trade_id"],
            "status_code": response.status_code,
            "latency_ms": elapsed_ms,
            "job_id": body.get("job_id") if isinstance(body, dict) else None,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - load test records every failure mode
        return {
            "trade_id": payload["trade_id"],
            "status_code": 0,
            "latency_ms": (time.monotonic() - started) * 1000.0,
            "job_id": None,
            "error": str(exc),
        }


async def audit_signal_jobs(trade_ids: list[str]) -> dict[str, Any]:
    """Report signal_jobs rows for the burst so queue drain can be inspected."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.db.session import create_engine_from_settings

    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(
                text(
                    "SELECT trade_id, status, attempt_count, worker_id, last_error "
                    "FROM signal_jobs WHERE trade_id = ANY(:tids)"
                ),
                {"tids": trade_ids},
            )
            rows = [dict(row) for row in result.mappings().all()]
    finally:
        await engine.dispose()

    status_counts: dict[str, int] = {}
    worker_counts: dict[str, int] = {}
    for row in rows:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
        worker = row["worker_id"] or "unclaimed"
        worker_counts[worker] = worker_counts.get(worker, 0) + 1

    persisted = {row["trade_id"] for row in rows}
    return {
        "expected": len(trade_ids),
        "persisted": len(rows),
        "missing_trade_ids": [tid for tid in trade_ids if tid not in persisted],
        "status_counts": status_counts,
        "worker_counts": worker_counts,
        "errors": [r["last_error"] for r in rows if r["last_error"]],
    }


async def run_burst(
    url: str, count: int, prefix: str, audit: bool, settle_sec: float
) -> dict[str, Any]:
    """Fire the burst concurrently and summarize ingestion behavior."""
    payloads = generate_payloads(count, prefix)
    trade_ids = [p["trade_id"] for p in payloads]

    logger.info("Sending %d concurrent signals to %s ...", count, url)
    limits = httpx.Limits(max_keepalive_connections=200, max_connections=500)
    timeout = httpx.Timeout(30.0, connect=10.0)

    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        started = time.monotonic()
        results = await asyncio.gather(*(send_one(client, url, p) for p in payloads))
        duration = time.monotonic() - started

    latencies = [r["latency_ms"] for r in results]
    accepted = sum(1 for r in results if 200 <= r["status_code"] < 300)

    report: dict[str, Any] = {
        "target_url": url,
        "count": count,
        "duration_sec": duration,
        "throughput_rps": count / max(0.001, duration),
        "http": {
            "accepted_2xx": accepted,
            "client_4xx": sum(1 for r in results if 400 <= r["status_code"] < 500),
            "server_5xx": sum(1 for r in results if 500 <= r["status_code"] < 600),
            "transport_errors": sum(1 for r in results if r["error"]),
        },
        "latency_ms": {
            "min": min(latencies),
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
            "p99": percentile(latencies, 99),
            "max": max(latencies),
            "avg": sum(latencies) / len(latencies),
        },
    }

    if audit:
        logger.info("Waiting %.1fs for workers before auditing signal_jobs ...", settle_sec)
        await asyncio.sleep(settle_sec)
        report["signal_jobs"] = await audit_signal_jobs(trade_ids)

    report["passed"] = (
        accepted == count and report["latency_ms"]["max"] < ACK_LATENCY_BUDGET_MS
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_TARGET_URL, help="Webhook endpoint")
    parser.add_argument("--count", type=int, default=150, help="Number of signals")
    parser.add_argument("--prefix", default="BURST", help="trade_id prefix")
    parser.add_argument(
        "--audit", action="store_true", help="Query signal_jobs after the burst"
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=3.0,
        help="Seconds to wait before the signal_jobs audit",
    )
    args = parser.parse_args()

    report = asyncio.run(
        run_burst(args.url, args.count, args.prefix, args.audit, args.settle)
    )
    print(json.dumps(report, indent=2, default=str))

    if report["passed"]:
        logger.info("PASS: all signals acked within %.0fms.", ACK_LATENCY_BUDGET_MS)
        return 0
    logger.error("FAIL: unaccepted requests or ack latency above budget.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
