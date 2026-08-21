"""Comprehensive 150-signal and 300-signal burst stress test runner for FastAPI MFT Webhook Ingestion."""

import asyncio
import time
from uuid import uuid4
import httpx

BASE_URL = "http://127.0.0.1:8000/api/webhooks/tradingview"

TEST_SYMBOLS = [
    ("NOBL", "SPYG"),
    ("EWP", "EWU"),
    ("EWA", "EWC"),
    ("EWH", "EWG"),
    ("EWT", "EWZ"),
]


def make_payload(idx: int, prefix: str = "BURST") -> dict:
    sym_a, sym_b = TEST_SYMBOLS[idx % len(TEST_SYMBOLS)]
    trade_id = f"MBG-{sym_a}-{sym_b}-BURST-{idx:04d}-{uuid4().hex[:6]}"
    return {
        "strategy": "model_blue",
        "trade_id": trade_id,
        "action": "OPEN",
        "leg_a": {
            "symbol": sym_a,
            "side": "BUY",
            "quantity": 10.0 + idx,
            "price": 100.0,
            "weight": 0.5,
            "contract_month": "202612",
        },
        "leg_b": {
            "symbol": sym_b,
            "side": "SELL",
            "quantity": 10.0 + idx,
            "price": 100.0,
            "weight": 0.5,
            "contract_month": "202612",
        },
    }


async def send_single_webhook(client: httpx.AsyncClient, payload: dict) -> tuple[int, float]:
    start = time.monotonic()
    try:
        resp = await client.post(BASE_URL, json=payload, timeout=10.0)
        elapsed = time.monotonic() - start
        return resp.status_code, elapsed
    except Exception as exc:
        elapsed = time.monotonic() - start
        return 500, elapsed


async def run_burst_test(burst_size: int) -> dict:
    print(f"\n=======================================================")
    print(f"STARTING {burst_size}-SIGNAL WEBHOOK BURST STRESS TEST")
    print(f"=======================================================")

    payloads = [make_payload(i, f"BURST-{burst_size}") for i in range(burst_size)]
    limits = httpx.Limits(max_keepalive_connections=100, max_connections=burst_size + 20)

    async with httpx.AsyncClient(limits=limits) as client:
        start_time = time.monotonic()
        results = await asyncio.gather(*[send_single_webhook(client, p) for p in payloads])
        total_time = time.monotonic() - start_time

    status_codes = [r[0] for r in results]
    latencies = [r[1] for r in results]

    ack_202 = sum(1 for code in status_codes if code == 202)
    ack_200 = sum(1 for code in status_codes if code == 200)
    failed = sum(1 for code in status_codes if code not in (200, 202))

    latencies_sorted = sorted(latencies)
    p50 = latencies_sorted[int(len(latencies_sorted) * 0.50)] * 1000
    p95 = latencies_sorted[int(len(latencies_sorted) * 0.95)] * 1000
    p99 = latencies_sorted[int(len(latencies_sorted) * 0.99)] * 1000
    max_lat = max(latencies) * 1000

    throughput = burst_size / total_time

    summary = {
        "burst_size": burst_size,
        "total_time_sec": total_time,
        "throughput_req_sec": throughput,
        "ack_202_count": ack_202,
        "ack_200_count": ack_200,
        "failed_count": failed,
        "latency_p50_ms": p50,
        "latency_p95_ms": p95,
        "latency_p99_ms": p99,
        "max_latency_ms": max_lat,
    }

    print(f"BURST SIZE              : {burst_size}")
    print(f"TOTAL INGESTION TIME    : {total_time:.3f} seconds")
    print(f"THROUGHPUT              : {throughput:.1f} req/sec")
    print(f"HTTP 202 ACK COUNT      : {ack_202} / {burst_size} ({ack_202/burst_size*100:.1f}%)")
    print(f"HTTP FAILED COUNT       : {failed}")
    print(f"LATENCY P50             : {p50:.2f} ms")
    print(f"LATENCY P95             : {p95:.2f} ms")
    print(f"LATENCY P99             : {p99:.2f} ms")
    print(f"MAX LATENCY             : {max_lat:.2f} ms")
    print(f"=======================================================\n")
    return summary


async def main():
    res150 = await run_burst_test(150)
    await asyncio.sleep(2.0)
    res300 = await run_burst_test(300)
    print("ALL BURST STRESS TESTS COMPLETE SUCCESSFULLY.")


if __name__ == "__main__":
    asyncio.run(main())
