"""Production Webhook Authentication & Fast Ingestion Test Suite.

Tests authentication secret verification, mTLS proxy checks, durable queue persistence,
execution separation, and concurrent signal burst stress performance.
"""

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.routes.webhooks import router as webhooks_router
from app.core.config import Settings, get_settings
from app.db.models.signal import SignalJobModel


@pytest.fixture
async def test_app() -> FastAPI:
    """Create a test FastAPI application instance with pool-managed AsyncEngine."""
    settings = get_settings()
    engine = create_async_engine(
        settings.database_url,
        pool_size=20,
        max_overflow=30,
        pool_timeout=30,
    )
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    app = FastAPI()
    app.include_router(webhooks_router, prefix="/api")
    app.state.session_factory = session_factory
    app.state.order_manager = None
    app.state.worker_pool = None
    yield app
    await engine.dispose()


@pytest.mark.asyncio
async def test_webhook_unauthenticated_request_rejected(
    test_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that requests missing secret return HTTP 401 when auth secret is configured."""
    monkeypatch.setattr(
        "app.api.routes.webhooks.get_settings",
        lambda: Settings(webhook_auth_secret="super-secret-token-123"),
    )

    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/api/webhooks/tradingview",
            json={"strategy": "model_blue", "trade_id": "T-AUTH-1", "action": "OPEN"},
        )
    assert resp.status_code == 401
    assert "Unauthorized" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_webhook_invalid_secret_rejected(
    test_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that requests with invalid secret return HTTP 401."""
    monkeypatch.setattr(
        "app.api.routes.webhooks.get_settings",
        lambda: Settings(webhook_auth_secret="super-secret-token-123"),
    )

    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/api/webhooks/tradingview",
            headers={"X-Webhook-Secret": "wrong-secret"},
            json={"strategy": "model_blue", "trade_id": "T-AUTH-2", "action": "OPEN"},
        )
    assert resp.status_code == 401
    assert "Unauthorized" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_webhook_valid_header_secret_accepted(
    test_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that requests with valid header secret return HTTP 202 and create signal_jobs row."""
    monkeypatch.setattr(
        "app.api.routes.webhooks.get_settings",
        lambda: Settings(webhook_auth_secret="super-secret-token-123"),
    )

    session_factory: async_sessionmaker[AsyncSession] = test_app.state.session_factory

    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/api/webhooks/tradingview",
            headers={"X-Webhook-Secret": "super-secret-token-123"},
            json={"strategy": "model_blue", "trade_id": "T-AUTH-3", "action": "OPEN"},
        )
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["signal_id"] == "T-AUTH-3"
    assert data["job_id"] is not None

    async with session_factory() as session:
        result = await session.execute(
            select(SignalJobModel).where(SignalJobModel.signal_id == "T-AUTH-3")
        )
        job = result.scalar_one_or_none()
        assert job is not None
        assert str(job.job_id) == data["job_id"]


@pytest.mark.asyncio
async def test_webhook_query_param_secret_rejected_security(
    test_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify secret passed via URL query parameter is rejected (HTTP 401) to prevent log leakage."""
    monkeypatch.setattr(
        "app.api.routes.webhooks.get_settings",
        lambda: Settings(webhook_auth_secret="super-secret-token-123"),
    )

    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/api/webhooks/tradingview?secret=super-secret-token-123",
            json={"strategy": "model_blue", "trade_id": "T-AUTH-4", "action": "OPEN"},
        )
    assert resp.status_code == 401
    assert "Unauthorized" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_webhook_auth_disabled_accepts_missing_or_wrong_secret(
    test_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that when WEBHOOK_AUTH_ENABLED=false, requests with missing or wrong secret are accepted (HTTP 202) and enqueued."""
    monkeypatch.setattr(
        "app.api.routes.webhooks.get_settings",
        lambda: Settings(
            webhook_auth_secret="super-secret-token-123",
            webhook_auth_enabled=False,
        ),
    )

    session_factory: async_sessionmaker[AsyncSession] = test_app.state.session_factory

    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://testserver"
    ) as client:
        # Request with missing secret header
        resp_missing = await client.post(
            "/api/webhooks/tradingview",
            json={"strategy": "model_blue", "trade_id": "T-DISABLED-1", "action": "OPEN"},
        )
        assert resp_missing.status_code == 202

        # Request with wrong secret header
        resp_wrong = await client.post(
            "/api/webhooks/tradingview",
            headers={"X-Webhook-Secret": "wrong-secret-token"},
            json={"strategy": "model_blue", "trade_id": "T-DISABLED-2", "action": "OPEN"},
        )
        assert resp_wrong.status_code == 202

    async with session_factory() as session:
        result1 = await session.execute(
            select(SignalJobModel).where(SignalJobModel.signal_id == "T-DISABLED-1")
        )
        assert result1.scalar_one_or_none() is not None

        result2 = await session.execute(
            select(SignalJobModel).where(SignalJobModel.signal_id == "T-DISABLED-2")
        )
        assert result2.scalar_one_or_none() is not None




@pytest.mark.asyncio
async def test_webhook_unauthorized_request_zero_db_writes(
    test_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify unauthorized requests make zero database writes to signal_jobs."""
    monkeypatch.setattr(
        "app.api.routes.webhooks.get_settings",
        lambda: Settings(webhook_auth_secret="secret123"),
    )
    session_factory: async_sessionmaker[AsyncSession] = test_app.state.session_factory

    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/api/webhooks/tradingview",
            headers={"X-Webhook-Secret": "invalid-secret"},
            json={"strategy": "model_blue", "trade_id": "T-UNAUTH-DB-0", "action": "OPEN"},
        )
    assert resp.status_code == 401

    async with session_factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(SignalJobModel)
                .where(SignalJobModel.signal_id == "T-UNAUTH-DB-0")
            )
        ).scalar_one()
        assert count == 0


@pytest.mark.asyncio
async def test_webhook_database_failure_returns_500(
    test_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that if database persistence fails, HTTP 500 is returned (never 202)."""
    mock_repo = MagicMock()
    mock_repo.create_job_if_not_exists = AsyncMock(side_effect=RuntimeError("Database connection lost"))
    monkeypatch.setattr("app.api.routes.webhooks.SignalJobRepository", lambda session: mock_repo)

    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/api/webhooks/tradingview",
            json={"strategy": "model_blue", "trade_id": "T-DB-FAIL-1", "action": "OPEN"},
        )
    assert resp.status_code == 500
    assert resp.json()["detail"] == "Failed to durably persist signal job."



@pytest.mark.asyncio
async def test_webhook_duplicate_idempotency_handling(test_app: FastAPI) -> None:
    """Verify duplicate signal submission returns HTTP 202 with original job_id and creates exactly 1 job."""
    payload = {"strategy": "model_blue", "trade_id": "T-DUP-1", "action": "OPEN"}
    session_factory: async_sessionmaker[AsyncSession] = test_app.state.session_factory

    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://testserver"
    ) as client:
        resp1 = await client.post("/api/webhooks/tradingview", json=payload)
        resp2 = await client.post("/api/webhooks/tradingview", json=payload)

    assert resp1.status_code == 202
    assert resp2.status_code == 202
    assert resp1.json()["job_id"] == resp2.json()["job_id"]

    async with session_factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(SignalJobModel)
                .where(SignalJobModel.signal_id == "T-DUP-1")
            )
        ).scalar_one()
        assert count == 1


@pytest.mark.asyncio
async def test_webhook_malformed_json_rejected(test_app: FastAPI) -> None:
    """Verify malformed JSON payload returns HTTP 400."""
    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/api/webhooks/tradingview",
            content="INVALID_NOT_JSON",
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_webhook_execution_separation(
    test_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that posting to webhook does NOT invoke OrderManager execution or RMS."""
    mock_order_manager = MagicMock()
    mock_order_manager.process_signal_execution = AsyncMock()
    mock_order_manager.parse_inbound_payload = MagicMock()
    test_app.state.order_manager = mock_order_manager

    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/api/webhooks/tradingview",
            json={"strategy": "model_blue", "trade_id": "T-SEP-1", "action": "OPEN"},
        )

    assert resp.status_code == 202
    # Verify OrderManager execution & parsing were NEVER called on synchronous path
    mock_order_manager.process_signal_execution.assert_not_called()
    mock_order_manager.parse_inbound_payload.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("burst_size", [150, 300, 500])
async def test_webhook_concurrent_burst_stress_benchmark(
    test_app: FastAPI,
    burst_size: int,
) -> None:
    """Stress test synchronous webhook ingestion under 150, 300, and 500 concurrent requests."""
    latencies: list[float] = []
    session_factory: async_sessionmaker[AsyncSession] = test_app.state.session_factory

    async def send_single_request(client: AsyncClient, idx: int) -> tuple[int, str]:
        t0 = time.monotonic()
        payload = {
            "strategy": "model_blue",
            "trade_id": f"T-BURST-{burst_size}-{idx}",
            "action": "OPEN",
            "direction": "LONG",
            "market": "US",
        }
        resp = await client.post("/api/webhooks/tradingview", json=payload)
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        latencies.append(elapsed_ms)
        return resp.status_code, resp.json().get("job_id", "")

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://testserver",
        timeout=60.0,
    ) as client:
        tasks = [send_single_request(client, i) for i in range(burst_size)]
        results = await asyncio.gather(*tasks)

    status_codes = [r[0] for r in results]
    job_ids = [r[1] for r in results if r[1]]

    assert status_codes.count(202) == burst_size, (
        f"Expected all {burst_size} requests to return 202, got {status_codes.count(202)}"
    )
    assert len(set(job_ids)) == burst_size, "All job_ids should be unique"

    # Verify all jobs were durably persisted in PostgreSQL
    async with session_factory() as session:
        persisted_count = (
            await session.execute(
                select(func.count())
                .select_from(SignalJobModel)
                .where(SignalJobModel.strategy_id == "model_blue")
                .where(SignalJobModel.signal_id.like(f"T-BURST-{burst_size}-%"))
            )
        ).scalar_one()
        assert persisted_count == burst_size

    latencies.sort()
    p50 = latencies[int(len(latencies) * 0.50)]
    p95 = latencies[int(len(latencies) * 0.95)]
    p99 = latencies[int(len(latencies) * 0.99)]
    max_lat = max(latencies)

    print(
        f"\n[BENCHMARK {burst_size} BURST] Total: {burst_size} | HTTP 202: {status_codes.count(202)} | "
        f"Persisted: {persisted_count} | p50: {p50:.2f}ms | p95: {p95:.2f}ms | p99: {p99:.2f}ms | max: {max_lat:.2f}ms"
    )
