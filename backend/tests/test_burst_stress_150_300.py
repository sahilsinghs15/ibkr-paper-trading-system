"""150-Signal and 300-Signal Burst Stress Integration Tests against FastAPI Webhook Pipeline."""

import asyncio
import time
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.webhook_ingest import app as ingest_app


def make_payload(idx: int, prefix: str = "BURST") -> dict:
    symbols = [("NOBL", "SPY"), ("EWP", "EWU"), ("XLF", "XLI"), ("EWA", "EWC"), ("AAPL", "MSFT")]
    sym_a, sym_b = symbols[idx % len(symbols)]
    trade_id = f"MBG-{sym_a}-{sym_b}-{prefix}-{idx:04d}-{uuid4().hex[:6]}"
    return {
        "strategy": "burst_ingest",
        "action": "OPEN",
        "trade_id": trade_id,
        "direction": 1 if (idx % 2 == 0) else -1,
        "market": "SMART",
        "buckets": [
            {
                "underlying": sym_a,
                "legs": [
                    {
                        "instrument_type": "STK",
                        "side": "BUY",
                        "weight": 1.0,
                        "price": "50.00",
                    }
                ],
            },
            {
                "underlying": sym_b,
                "legs": [
                    {
                        "instrument_type": "STK",
                        "side": "SELL",
                        "weight": 1.0,
                        "price": "100.00",
                    }
                ],
            },
        ],
    }


@pytest.mark.asyncio
async def test_150_signal_burst_webhook_ingestion(session_factory: async_sessionmaker[AsyncSession]):
    """Verify 150 concurrent webhook signals ingest in < 5.0s with 100% 202 ACKs."""
    ingest_app.state.session_factory = session_factory
    count = 150
    payloads = [make_payload(i, "BURST150") for i in range(count)]
    trade_ids = [p["trade_id"] for p in payloads]

    transport = ASGITransport(app=ingest_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        start_time = time.monotonic()
        responses = await asyncio.gather(
            *[client.post("/api/webhooks/tradingview", json=p) for p in payloads]
        )
        total_duration = time.monotonic() - start_time

    status_codes = [r.status_code for r in responses]
    assert all(sc == 202 for sc in status_codes), f"Expected all 202 ACKs, got {set(status_codes)}"
    assert total_duration < 5.0, f"150 signal burst took too long: {total_duration:.2f}s"

    # Allow background persistence tasks to complete commit
    await asyncio.sleep(0.5)

    # Audit database signal_jobs persistence
    async with session_factory() as session:
        res = await session.execute(
            text("SELECT COUNT(DISTINCT trade_id) FROM signal_jobs WHERE trade_id = ANY(:tids)"),
            {"tids": trade_ids},
        )
        persisted_count = res.scalar_one()

    assert persisted_count == count, f"Expected {count} persisted signal_jobs rows, found {persisted_count}"


@pytest.mark.asyncio
async def test_300_signal_burst_webhook_ingestion(session_factory: async_sessionmaker[AsyncSession]):
    """Verify 300 concurrent webhook signals ingest without blocking or error."""
    ingest_app.state.session_factory = session_factory
    count = 300
    payloads = [make_payload(i, "BURST300") for i in range(count)]
    trade_ids = [p["trade_id"] for p in payloads]

    transport = ASGITransport(app=ingest_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        start_time = time.monotonic()
        responses = await asyncio.gather(
            *[client.post("/api/webhooks/tradingview", json=p) for p in payloads]
        )
        total_duration = time.monotonic() - start_time

    status_codes = [r.status_code for r in responses]
    assert all(sc == 202 for sc in status_codes), f"Expected all 202 ACKs, got {set(status_codes)}"
    assert total_duration < 10.0, f"300 signal burst took too long: {total_duration:.2f}s"

    await asyncio.sleep(0.5)

    async with session_factory() as session:
        res = await session.execute(
            text("SELECT COUNT(DISTINCT trade_id) FROM signal_jobs WHERE trade_id = ANY(:tids)"),
            {"tids": trade_ids},
        )
        persisted_count = res.scalar_one()

    assert persisted_count == count, f"Expected {count} persisted signal_jobs rows, found {persisted_count}"
