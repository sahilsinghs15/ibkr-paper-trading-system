import asyncio
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest
from src.worker_pool import MarginCacheWorkerPool
from src.models import Instrument, MarginResult, utc_now_iso


@pytest.mark.asyncio
async def test_worker_pool_concurrency_limit():
    # Track max concurrency
    concurrent = 0
    max_concurrent = 0
    lock = asyncio.Lock()

    async def fetch(inst: Instrument) -> MarginResult:
        nonlocal concurrent, max_concurrent
        async with lock:
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
        await asyncio.sleep(0.05)
        async with lock:
            concurrent -= 1
        return MarginResult(instrument_type=inst.instrument_type, symbol=inst.symbol, exchange=inst.exchange, currency=inst.currency, status="ok", timestamp_utc=utc_now_iso())

    pool = MarginCacheWorkerPool(worker_count=2)
    insts = [Instrument("ETF", f"S{i}", "ARCA", "USD") for i in range(6)]
    results = await pool.run(insts, fetch)
    assert len(results)==6
    assert max_concurrent <= 2

@pytest.mark.asyncio
async def test_worker_pool_handles_failures():
    async def fetch(inst: Instrument) -> MarginResult:
        if inst.symbol=="FAIL":
            raise RuntimeError("boom")
        return MarginResult(instrument_type=inst.instrument_type, symbol=inst.symbol, exchange=inst.exchange, currency=inst.currency, status="ok", timestamp_utc=utc_now_iso())
    pool = MarginCacheWorkerPool(worker_count=2)
    insts = [Instrument("ETF","OK1","ARCA","USD"), Instrument("ETF","FAIL","ARCA","USD"), Instrument("ETF","OK2","ARCA","USD")]
    results = await pool.run(insts, fetch)
    assert len(results)==3
    assert sum(1 for r in results if r.status=="failed")==1

def test_invalid_worker_count():
    with pytest.raises(ValueError):
        MarginCacheWorkerPool(worker_count=0)
