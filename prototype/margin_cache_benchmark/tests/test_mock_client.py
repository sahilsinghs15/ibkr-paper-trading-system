import asyncio
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest
from src.ibkr_client.mock_client import MockIBKRClient
from src.rate_limiter import PrototypeRateLimiter
from src.models import Instrument


@pytest.mark.asyncio
async def test_mock_success_path():
    limiter = PrototypeRateLimiter(rate_per_sec=50, burst=50)
    client = MockIBKRClient(rate_limiter=limiter, failure_rate=0, pacing_error_rate=0, timeout_rate=0)
    await client.connect()
    assert client.is_connected()
    inst = Instrument("ETF","SPY","ARCA","USD")
    con_id, ms = await client.resolve_contract(inst)
    assert con_id is not None
    init, maint, ms2 = await client.fetch_margin(inst, con_id)
    assert init and maint

@pytest.mark.asyncio
async def test_mock_pacing_error():
    limiter = PrototypeRateLimiter(rate_per_sec=100, burst=100)
    client = MockIBKRClient(rate_limiter=limiter, failure_rate=0, pacing_error_rate=1.0, timeout_rate=0)
    await client.connect()
    inst = Instrument("ETF","SPY","ARCA","USD")
    with pytest.raises(RuntimeError, match="pacing"):
        await client.resolve_contract(inst)

@pytest.mark.asyncio
async def test_mock_rate_limiter_integration():
    limiter = PrototypeRateLimiter(rate_per_sec=10, burst=10)
    client = MockIBKRClient(rate_limiter=limiter, failure_rate=0, pacing_error_rate=0, timeout_rate=0)
    await client.connect()
    # fire 12 quickly — some should be delayed but not fail
    tasks = [client.resolve_contract(Instrument("ETF", f"S{i}", "ARCA","USD")) for i in range(12)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    # At least some succeeded
    successes = sum(1 for r in results if not isinstance(r, Exception))
    assert successes >= 10
