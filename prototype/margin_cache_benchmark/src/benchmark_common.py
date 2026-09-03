"""Shared helpers for benchmarks."""

from __future__ import annotations

import statistics
import time
from typing import Any

from .models import BenchmarkStats, MarginResult


def compute_stats(
    approach: str,
    instruments: int,
    workers: int,
    rate_limit: float,
    total_time_sec: float,
    results: list[MarginResult],
    pacing_errors: int,
    label: str,
) -> BenchmarkStats:
    latencies = [r.total_ms for r in results if r.status == "ok" and r.total_ms > 0]
    # fallback to all if no ok
    if not latencies:
        latencies = [r.total_ms for r in results if r.total_ms > 0]
    if latencies:
        avg = statistics.mean(latencies)
        median = statistics.median(latencies)
        sorted_l = sorted(latencies)
        idx = int(0.95 * len(sorted_l))
        p95 = sorted_l[min(idx, len(sorted_l) - 1)]
    else:
        avg = median = p95 = 0.0

    contract_avgs = [r.contract_resolve_ms for r in results if r.contract_resolve_ms > 0]
    margin_avgs = [r.margin_ms for r in results if r.margin_ms > 0]
    successes = sum(1 for r in results if r.status == "ok")
    failures = len(results) - successes
    total_requests = len(results) * 2  # contract + margin per instrument
    actual_rate = (total_requests / total_time_sec) if total_time_sec > 0 else 0.0

    return BenchmarkStats(
        approach=approach,
        instruments=instruments,
        workers=workers,
        rate_limit=rate_limit,
        total_time_sec=total_time_sec,
        successes=successes,
        failures=failures,
        avg_latency_ms=avg,
        median_latency_ms=median,
        p95_latency_ms=p95,
        total_requests=total_requests,
        pacing_errors=pacing_errors,
        retries=0,
        contract_resolve_avg_ms=statistics.mean(contract_avgs) if contract_avgs else 0.0,
        margin_avg_ms=statistics.mean(margin_avgs) if margin_avgs else 0.0,
        actual_rate_per_sec=actual_rate,
        label=label,
    )
