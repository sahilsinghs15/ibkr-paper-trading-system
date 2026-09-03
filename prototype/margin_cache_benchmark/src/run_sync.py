"""Synchronous benchmark — one instrument at a time.

CSV -> Instrument 1 -> Resolve -> Margin -> Instrument 2 ...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from .benchmark_common import compute_stats
from .cache_writer import write_cache_csv
from .config import BenchmarkConfig
from .csv_loader import load_instruments
from .ibkr_client.mock_client import MockIBKRClient
from .models import MarginResult, utc_now_iso


async def _fetch_one_sync(
    instrument,
    client,
    contract_cache: dict[str, int | None],
    use_cache: bool,
) -> MarginResult:
    total_start = time.monotonic()
    # Contract resolution
    contract_ms = 0.0
    con_id: int | None = None
    cached = False
    cache_key = instrument.key()
    if use_cache and cache_key in contract_cache:
        con_id = contract_cache[cache_key]
        cached = True
        contract_ms = 0.0
    else:
        try:
            con_id, contract_ms = await client.resolve_contract(instrument)
            if use_cache:
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


async def run_sync(
    instruments,
    client,
    rate_limiter=None,
    use_cache: bool = False,
    label: str = "MOCK RESULT",
) -> tuple[list[MarginResult], object]:
    t0 = time.monotonic()
    cache: dict[str, int | None] = {}
    results: list[MarginResult] = []
    for inst in instruments:
        r = await _fetch_one_sync(inst, client, cache, use_cache=use_cache)
        results.append(r)
    total = time.monotonic() - t0
    pacing = getattr(client, "pacing_errors", 0) + (getattr(rate_limiter, "metrics", None).pacing_errors if rate_limiter and hasattr(rate_limiter, "metrics") else 0)
    # Prefer rate_limiter pacing if available
    if rate_limiter is not None and hasattr(rate_limiter, "metrics"):
        pacing = rate_limiter.metrics.pacing_errors or pacing
    stats = compute_stats("Synchronous", len(instruments), 1, getattr(rate_limiter, "rate_per_sec", 10.0) if rate_limiter else 10.0, total, results, pacing, label)
    return results, stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Synchronous margin benchmark (MOCK or REAL)")
    parser.add_argument("--csv", type=str, default=None, help="Instrument CSV path")
    parser.add_argument("--limit", type=int, default=None, help="Limit instruments (smoke test)")
    parser.add_argument("--rate", type=float, default=10.0, help="Rate limit req/sec")
    parser.add_argument("--use-cache", action="store_true", help="Enable contract warm-cache mode")
    parser.add_argument("--mock", action="store_true", default=True, help="Use mock client (default)")
    parser.add_argument("--real", action="store_true", help="Use real IBKR Gateway :4002 (whatIf)")
    parser.add_argument("--out-cache", type=str, default="results/cache_sync.csv")
    parser.add_argument("--out-stats", type=str, default="results/stats_sync.json")
    args = parser.parse_args()

    base = Path(__file__).resolve().parent.parent
    csv_path = Path(args.csv) if args.csv else base / "data" / "instruments.csv"
    # Try alternative: prototype root data
    if not csv_path.exists():
        alt = Path.cwd() / args.csv if args.csv else Path.cwd() / "data" / "instruments.csv"
        if alt.exists():
            csv_path = alt

    instruments = load_instruments(str(csv_path))
    if args.limit:
        instruments = instruments[: args.limit]

    cache_mode = "warm" if args.use_cache else "cold"
    print(f"[SYNC:{cache_mode}] {len(instruments)} instruments, rate={args.rate}/s, csv={csv_path}")

    async def _run():
        from .rate_limiter import PrototypeRateLimiter

        limiter = PrototypeRateLimiter(rate_per_sec=args.rate)
        use_real = args.real and not args.mock
        # --real explicitly requests real; if both not set, default mock
        if args.real:
            use_real = True
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
                results, stats = await run_sync(instruments, client, limiter, use_cache=args.use_cache, label=label)
            finally:
                await client.disconnect()
        else:
            client = MockIBKRClient(rate_limiter=limiter)
            await client.connect()
            results, stats = await run_sync(instruments, client, limiter, use_cache=args.use_cache, label="MOCK RESULT")

        out_cache = base / args.out_cache if not Path(args.out_cache).is_absolute() else Path(args.out_cache)
        out_stats = base / args.out_stats if not Path(args.out_stats).is_absolute() else Path(args.out_stats)
        write_cache_csv(results, out_cache)
        out_stats.parent.mkdir(parents=True, exist_ok=True)
        out_stats.write_text(json.dumps(stats.to_dict(), indent=2))
        print(json.dumps(stats.to_dict(), indent=2))
        print(f"Cache -> {out_cache}")
        print(f"Stats -> {out_stats}")

    asyncio.run(_run())


if __name__ == "__main__":
    main()
