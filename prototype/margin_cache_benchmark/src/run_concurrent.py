"""Concurrent benchmark — worker pool with rate limiter.

CSV queue -> MarginCacheWorkerPool (N workers) -> Rate Limiter -> IB Gateway
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from .benchmark_common import compute_stats
from .cache_writer import write_cache_csv
from .csv_loader import load_instruments
from .ibkr_client.mock_client import MockIBKRClient
from .models import MarginResult, utc_now_iso
from .rate_limiter import PrototypeRateLimiter
from .worker_pool import MarginCacheWorkerPool


async def run_concurrent(
    instruments,
    client,
    rate_limiter: PrototypeRateLimiter | None = None,
    workers: int = 2,
    use_cache: bool = False,
    label: str = "MOCK RESULT",
) -> tuple[list[MarginResult], object]:
    t0 = time.monotonic()
    # contract cache shared across workers
    contract_cache: dict[str, int | None] = {}
    cache_lock = asyncio.Lock()

    async def fetch_one(instrument) -> MarginResult:
        total_start = time.monotonic()
        cache_key = instrument.key()
        # contract resolve with cache check
        con_id: int | None = None
        contract_ms = 0.0
        cached = False
        if use_cache:
            async with cache_lock:
                if cache_key in contract_cache:
                    con_id = contract_cache[cache_key]
                    cached = True
        if not cached:
            try:
                con_id, contract_ms = await client.resolve_contract(instrument)
                if use_cache:
                    async with cache_lock:
                        contract_cache[cache_key] = con_id
            except Exception as e:
                elapsed = (time.monotonic() - total_start) * 1000
                return MarginResult(
                    instrument_type=instrument.instrument_type,
                    symbol=instrument.symbol,
                    exchange=instrument.exchange,
                    currency=instrument.currency,
                    con_id=None,
                    timestamp_utc=utc_now_iso(),
                    status="failed",
                    error=f"contract: {e}"[:500],
                    contract_resolve_ms=contract_ms,
                    margin_ms=0.0,
                    total_ms=elapsed,
                    cached_contract=cached,
                )
        try:
            init, maint, margin_ms = await client.fetch_margin(instrument, con_id)
            total_ms = (time.monotonic() - total_start) * 1000
            return MarginResult(
                instrument_type=instrument.instrument_type,
                symbol=instrument.symbol,
                exchange=instrument.exchange,
                currency=instrument.currency,
                con_id=con_id,
                initial_margin=init,
                maintenance_margin=maint,
                timestamp_utc=utc_now_iso(),
                status="ok",
                error="",
                contract_resolve_ms=contract_ms,
                margin_ms=margin_ms,
                total_ms=total_ms,
                cached_contract=cached,
            )
        except Exception as e:
            total_ms = (time.monotonic() - total_start) * 1000
            return MarginResult(
                instrument_type=instrument.instrument_type,
                symbol=instrument.symbol,
                exchange=instrument.exchange,
                currency=instrument.currency,
                con_id=con_id,
                timestamp_utc=utc_now_iso(),
                status="failed",
                error=f"margin: {e}"[:500],
                contract_resolve_ms=contract_ms,
                margin_ms=0.0,
                total_ms=total_ms,
                cached_contract=cached,
            )

    pool = MarginCacheWorkerPool(worker_count=workers, rate_limiter=rate_limiter)
    results = await pool.run(instruments, fetch_one)
    total = time.monotonic() - t0
    pacing = 0
    if rate_limiter and hasattr(rate_limiter, "metrics"):
        pacing = rate_limiter.metrics.pacing_errors
    pacing = max(pacing, getattr(client, "pacing_errors", 0))
    rate_val = rate_limiter.rate_per_sec if rate_limiter else 10.0
    stats = compute_stats("Concurrent", len(instruments), workers, rate_val, total, results, pacing, label)
    return results, stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Concurrent margin benchmark")
    parser.add_argument("--csv", type=str, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--rate", type=float, default=10.0)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--use-cache", action="store_true")
    parser.add_argument("--mock", action="store_true", default=True)
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--out-cache", type=str, default=None)
    parser.add_argument("--out-stats", type=str, default=None)
    args = parser.parse_args()

    base = Path(__file__).resolve().parent.parent
    csv_path = Path(args.csv) if args.csv else base / "data" / "instruments.csv"
    if not csv_path.exists():
        alt = Path.cwd() / args.csv if args.csv else Path.cwd() / "data" / "instruments.csv"
        if alt.exists():
            csv_path = alt

    instruments = load_instruments(str(csv_path))
    if args.limit:
        instruments = instruments[: args.limit]

    cache_mode = "warm" if args.use_cache else "cold"
    print(f"[CONCURRENT:{cache_mode}] {len(instruments)} instruments, workers={args.workers}, rate={args.rate}/s")

    async def _run():
        limiter = PrototypeRateLimiter(rate_per_sec=args.rate)
        use_real = args.real
        if use_real:
            from .config import BenchmarkConfig
            from .ibkr_client.real_client import RealIBKRClient

            cfg = BenchmarkConfig.from_env()
            cfg = BenchmarkConfig(
                ib_host=cfg.ib_host,
                ib_port=cfg.ib_port,
                ib_client_id=cfg.ib_client_id,
                cache_rate_limit=args.rate,
                max_wait_sec=cfg.max_wait_sec,
                contract_details_timeout=cfg.contract_details_timeout,
                margin_timeout=cfg.margin_timeout,
                csv_path=str(csv_path),
            )
            client = RealIBKRClient(cfg, rate_limiter=limiter)  # type: ignore
            await client.connect()
            label = "REAL IBKR PAPER GATEWAY RESULT"
            try:
                results, stats = await run_concurrent(instruments, client, limiter, workers=args.workers, use_cache=args.use_cache, label=label)
            finally:
                await client.disconnect()
        else:
            client = MockIBKRClient(rate_limiter=limiter)
            await client.connect()
            results, stats = await run_concurrent(instruments, client, limiter, workers=args.workers, use_cache=args.use_cache, label="MOCK RESULT")

        out_cache = Path(args.out_cache) if args.out_cache else base / f"results/cache_concurrent_w{args.workers}.csv"
        out_stats = Path(args.out_stats) if args.out_stats else base / f"results/stats_concurrent_w{args.workers}.json"
        if not out_cache.is_absolute():
            out_cache = base / out_cache
        if not out_stats.is_absolute():
            out_stats = base / out_stats
        write_cache_csv(results, out_cache)
        out_stats.parent.mkdir(parents=True, exist_ok=True)
        out_stats.write_text(json.dumps(stats.to_dict(), indent=2))
        print(json.dumps(stats.to_dict(), indent=2))
        print(f"Cache -> {out_cache}")
        print(f"Stats -> {out_stats}")

    asyncio.run(_run())


if __name__ == "__main__":
    main()
