"""Automated 300+ signal burst load test for production MFT execution engine."""

import asyncio
import logging
import sys
import time
from typing import Any

import httpx
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("load_test_mft_burst")

DEFAULT_TARGET_URL = "http://127.0.0.1:8000/api/webhooks/tradingview"


def generate_burst_payloads(count: int = 300) -> list[dict[str, Any]]:
    """Generate `count` distinct pair-trading signal payloads."""
    payloads = []
    pairs = [("AAPL", "MSFT"), ("NVDA", "AMD"), ("GOOGL", "META"), ("AMZN", "NFLX"), ("JPM", "BAC")]
    for i in range(count):
        pair_a, pair_b = pairs[i % len(pairs)]
        trade_id = f"BURST-{i:04d}"
        payloads.append(
            {
                "strategy": "model_blue",
                "action": "OPEN",
                "trade_id": trade_id,
                "direction": "LONG",
                "market": "SMART",
                "buckets": [
                    {
                        "underlying": pair_a,
                        "legs": [
                            {
                                "instrument_type": "STK",
                                "side": "BUY",
                                "weight": 1.0,
                                "price": "150.00",
                            }
                        ],
                    },
                    {
                        "underlying": pair_b,
                        "legs": [
                            {
                                "instrument_type": "STK",
                                "side": "SELL",
                                "weight": 1.0,
                                "price": "300.00",
                            }
                        ],
                    },
                ],
            }
        )
    return payloads


async def send_signal(
    client: httpx.AsyncClient, target_url: str, payload: dict[str, Any], index: int
) -> tuple[int, float, dict[str, Any]]:
    start = time.monotonic()
    try:
        response = await client.post(target_url, json=payload)
        elapsed_ms = (time.monotonic() - start) * 1000.0
        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text}
        return response.status_code, elapsed_ms, body
    except Exception as exc:
        elapsed_ms = (time.monotonic() - start) * 1000.0
        return 500, elapsed_ms, {"error": str(exc)}


async def run_burst_test(target_url: str = DEFAULT_TARGET_URL, count: int = 300) -> bool:
    """Execute burst load test sending `count` webhooks concurrently."""
    payloads = generate_burst_payloads(count)
    logger.info("Starting burst load test sending %d signals to %s...", count, target_url)

    limits = httpx.Limits(max_keepalive_connections=100, max_connections=500)
    timeout = httpx.Timeout(15.0, connect=5.0)

    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        t0 = time.monotonic()
        tasks = [send_signal(client, target_url, payloads[i], i) for i in range(count)]
        results = await asyncio.gather(*tasks)
        total_duration = time.monotonic() - t0

    status_codes = [r[0] for r in results]
    latencies = [r[1] for r in results]

    accepted_count = status_codes.count(202) + status_codes.count(200)
    failed_count = count - accepted_count

    p50 = float(np.percentile(latencies, 50))
    p95 = float(np.percentile(latencies, 95))
    p99 = float(np.percentile(latencies, 99))
    max_lat = float(np.max(latencies))

    logger.info("=" * 60)
    logger.info("LOAD TEST RESULTS SUMMARY (%d Signals Burst)", count)
    logger.info("=" * 60)
    logger.info("Total Burst Ingestion Time : %.3f seconds", total_duration)
    logger.info("Throughput                  : %.1f signals/sec", count / max(0.001, total_duration))
    logger.info("HTTP Accepted (202/200)     : %d / %d (%.1f%%)", accepted_count, count, (accepted_count / count) * 100)
    logger.info("HTTP Failed                 : %d", failed_count)
    logger.info("Latency p50                 : %.2f ms", p50)
    logger.info("Latency p95                 : %.2f ms", p95)
    logger.info("Latency p99                 : %.2f ms", p99)
    logger.info("Max Latency                 : %.2f ms", max_lat)
    logger.info("=" * 60)

    # Success criteria: 100% accepted, max latency < 2000ms (NOT 90 seconds!)
    if accepted_count == count and max_lat < 2000.0:
        logger.info("LOAD TEST PASSED! Webhook ingress completed without holding connections.")
        return True
    else:
        logger.error("LOAD TEST FAILED! Unaccepted requests or excessive webhook latency.")
        return False


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TARGET_URL
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    success = asyncio.run(run_burst_test(url, count))
    sys.exit(0 if success else 1)
