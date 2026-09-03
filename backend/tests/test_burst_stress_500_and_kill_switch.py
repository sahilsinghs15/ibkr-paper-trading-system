"""500-Signal Burst & Concurrent Kill-Switch Priority Stress Integration Test."""

import asyncio
import time
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.main import app as trading_app
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
async def test_500_signal_burst_webhook_ingestion(session_factory: async_sessionmaker[AsyncSession]):
    """Verify 500 concurrent webhook signals ingest without connection error or rate limit crash."""
    ingest_app.state.session_factory = session_factory
    count = 500
    payloads = [make_payload(i, "BURST500") for i in range(count)]
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
    assert total_duration < 15.0, f"500 signal burst took too long: {total_duration:.2f}s"

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
async def test_simultaneous_300_signals_and_kill_switch(session_factory: async_sessionmaker[AsyncSession]):
    """Verify 300 signals and a concurrent Kill Switch trigger coexist without blocking."""
    ingest_app.state.session_factory = session_factory
    trading_app.state.session_factory = session_factory
    count = 300
    payloads = [make_payload(i, "BURST300KS") for i in range(count)]

    # Create account in DB for square-off endpoint
    from app.db.models.account import AccountModel
    ibkr_acc = f"DU{uuid4().hex[:6]}"
    async with session_factory() as session, session.begin():
        acc = AccountModel(name="SimultaneousKSTestAcc", ibkr_account=ibkr_acc, total_margin=Decimal("100000.00"))
        session.add(acc)
        await session.flush()
        acc_id = acc.id

    ingest_transport = ASGITransport(app=ingest_app)
    trading_transport = ASGITransport(app=trading_app)

    with (
        patch("app.broker.ibkr.tws_client.TWSClient.connect_and_start", return_value=True),
        patch("app.broker.ibkr.tws_client.TWSClient.disconnect_clean"),
        patch("app.services.worker_pool.ExecutionWorkerPool.start", new_callable=AsyncMock),
        patch("app.services.worker_pool.ExecutionWorkerPool.stop", new_callable=AsyncMock),
        patch(
            "app.services.position_reconciler.PositionReconciler.start",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.position_reconciler.PositionReconciler.stop",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.recovery.RecoveryManager.run_startup_recovery",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.order_manager.OrderManager.hydrate_live_pnl",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.order_manager.OrderManager.hydrate_runtime_from_db",
            new_callable=AsyncMock,
        ),
    ):
        async with (
            AsyncClient(transport=ingest_transport, base_url="http://test") as ingest_client,
            AsyncClient(transport=trading_transport, base_url="http://test") as trading_client,
        ):
            start_time = time.monotonic()

            # Fire 300 webhooks on ingest AND 1 Kill Switch on trading app simultaneously
            webhook_tasks = [
                ingest_client.post("/api/webhooks/tradingview", json=p) for p in payloads
            ]
            kill_switch_task = trading_client.post(
                f"/api/v1/config/accounts/{acc_id}/square-off"
            )
            responses = await asyncio.gather(*webhook_tasks, kill_switch_task)
            total_duration = time.monotonic() - start_time

    webhook_responses = responses[:count]
    kill_switch_response = responses[-1]

    assert all(r.status_code == 202 for r in webhook_responses)
    assert kill_switch_response.status_code == 202
    assert total_duration < 10.0, f"Simultaneous burst + Kill Switch took too long: {total_duration:.2f}s"
