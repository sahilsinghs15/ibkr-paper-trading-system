import asyncio
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest
from src.csv_loader import load_instruments
from src.ibkr_client.mock_client import MockIBKRClient
from src.rate_limiter import PrototypeRateLimiter
from src.run_sync import run_sync
from src.run_concurrent import run_concurrent


@pytest.mark.asyncio
async def test_sync_vs_concurrent_mock_small():
    csv_path = Path(__file__).resolve().parent.parent / "data" / "instruments_sample_20.csv"
    insts = load_instruments(str(csv_path))[:10]
    # sync
    limiter_sync = PrototypeRateLimiter(rate_per_sec=20, burst=20)
    client_sync = MockIBKRClient(rate_limiter=limiter_sync, failure_rate=0, pacing_error_rate=0, timeout_rate=0, contract_latency_ms=(5,10), margin_latency_ms=(5,10))
    await client_sync.connect()
    results_sync, stats_sync = await run_sync(insts, client_sync, limiter_sync, use_cache=False, label="MOCK RESULT")
    assert len(results_sync)==10
    assert stats_sync.successes==10

    # concurrent 2 workers
    limiter_conc = PrototypeRateLimiter(rate_per_sec=20, burst=20)
    client_conc = MockIBKRClient(rate_limiter=limiter_conc, failure_rate=0, pacing_error_rate=0, timeout_rate=0, contract_latency_ms=(5,10), margin_latency_ms=(5,10))
    await client_conc.connect()
    results_conc, stats_conc = await run_concurrent(insts, client_conc, limiter_conc, workers=2, use_cache=False, label="MOCK RESULT")
    assert len(results_conc)==10
    assert stats_conc.successes==10
    # concurrent should be faster due to overlapping latency
    # With 20/s rate and 10 insts, concurrent overhead low
    assert stats_conc.total_time_sec <= stats_sync.total_time_sec + 0.5  # allow small variance
