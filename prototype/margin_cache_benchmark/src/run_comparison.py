"""Run full comparison matrix and produce markdown/CSV summary."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
from pathlib import Path

from .csv_loader import load_instruments
from .ibkr_client.mock_client import MockIBKRClient
from .rate_limiter import PrototypeRateLimiter
from .run_concurrent import run_concurrent
from .run_sync import run_sync


async def run_matrix(csv_path: Path, rate: float, workers_list: list[int], limit: int | None, use_real: bool = False) -> list[dict]:
    instruments = load_instruments(str(csv_path))
    if limit:
        instruments = instruments[:limit]

    rows: list[dict] = []

    # Sync cold
    limiter = PrototypeRateLimiter(rate_per_sec=rate)
    if use_real:
        from .config import BenchmarkConfig
        from .ibkr_client.real_client import RealIBKRClient
        cfg = BenchmarkConfig.from_env()
        client = RealIBKRClient(cfg, rate_limiter=limiter)  # type: ignore
        await client.connect()
        try:
            _, stats = await run_sync(instruments, client, limiter, use_cache=False, label="REAL IBKR PAPER GATEWAY RESULT")
        finally:
            await client.disconnect()
    else:
        client = MockIBKRClient(rate_limiter=limiter)
        await client.connect()
        _, stats = await run_sync(instruments, client, limiter, use_cache=False, label="MOCK RESULT")
    rows.append(stats.to_dict())

    for w in workers_list:
        limiter = PrototypeRateLimiter(rate_per_sec=rate)
        if use_real:
            from .config import BenchmarkConfig
            from .ibkr_client.real_client import RealIBKRClient
            cfg = BenchmarkConfig.from_env()
            client = RealIBKRClient(cfg, rate_limiter=limiter)  # type: ignore
            await client.connect()
            try:
                _, stats = await run_concurrent(instruments, client, limiter, workers=w, use_cache=False, label="REAL IBKR PAPER GATEWAY RESULT")
            finally:
                await client.disconnect()
        else:
            client = MockIBKRClient(rate_limiter=limiter)
            await client.connect()
            _, stats = await run_concurrent(instruments, client, limiter, workers=w, use_cache=False, label="MOCK RESULT")
        rows.append(stats.to_dict())

    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=str, default=None)
    parser.add_argument("--rate", type=float, default=10.0)
    parser.add_argument("--workers", type=str, default="2,5,10")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--out", type=str, default="results/comparison.json")
    args = parser.parse_args()

    base = Path(__file__).resolve().parent.parent
    csv_path = Path(args.csv) if args.csv else base / "data" / "instruments.csv"
    workers_list = [int(x.strip()) for x in args.workers.split(",") if x.strip()]

    rows = asyncio.run(run_matrix(csv_path, args.rate, workers_list, args.limit, use_real=args.real))

    out = Path(args.out) if Path(args.out).is_absolute() else base / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2))

    # Print markdown table
    headers = ["Approach", "Instruments", "Workers", "Rate Limit", "Total Time", "Avg Latency", "P95", "Errors", "Pacing Errors"]
    print("| " + " | ".join(headers) + " |")
    print("| " + " | ".join(["---"] * len(headers)) + " |")
    for r in rows:
        print(f"| {r['approach']} | {r['instruments']} | {r['workers']} | {r['rate_limit']}/s | {r['total_time_sec']}s | {r['avg_latency_ms']}ms | {r['p95_latency_ms']}ms | {r['failures']} | {r['pacing_errors']} |")
    print(f"\nWritten to {out}")
    # Also CSV
    csv_out = out.with_suffix(".csv")
    with csv_out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"CSV -> {csv_out}")


if __name__ == "__main__":
    main()
