"""Production Webhook Authentication & Fast Ingestion Test Suite.

Tests authentication secret verification, mTLS proxy checks, durable queue persistence,
execution separation, and concurrent signal burst stress performance.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

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
        lambda: Settings(
            webhook_auth_secret="super-secret-token-123",
            webhook_auth_enabled=True,
        ),
    )

    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/api/webhooks/tradingview",
            json={"strategy": "webhook_auth_test", "trade_id": "T-AUTH-1", "action": "OPEN"},
        )
    assert resp.status_code == 401
    assert "Unauthorized" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_webhook_unconfigured_secret_fails_closed(
    test_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enabled auth with no secret must 401 — same shape as emergency kill-switch."""
    monkeypatch.setattr(
        "app.api.routes.webhooks.get_settings",
        lambda: Settings(
            webhook_auth_secret=None,
            webhook_auth_enabled=True,
        ),
    )

    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/api/webhooks/tradingview",
            json={"strategy": "webhook_auth_test", "trade_id": "T-AUTH-UNSET", "action": "OPEN"},
        )
    assert resp.status_code == 401
    assert "not configured" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_webhook_invalid_secret_rejected(
    test_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that requests with invalid secret return HTTP 401."""
    monkeypatch.setattr(
        "app.api.routes.webhooks.get_settings",
        lambda: Settings(
            webhook_auth_secret="super-secret-token-123",
            webhook_auth_enabled=True,
        ),
    )

    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/api/webhooks/tradingview",
            headers={"X-Webhook-Secret": "wrong-secret"},
            json={"strategy": "webhook_auth_test", "trade_id": "T-AUTH-2", "action": "OPEN"},
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
        lambda: Settings(
            webhook_auth_secret="super-secret-token-123",
            webhook_auth_enabled=True,
        ),
    )

    session_factory: async_sessionmaker[AsyncSession] = test_app.state.session_factory
    trade_id = f"T-AUTH-3-{uuid4().hex[:8]}"

    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/api/webhooks/tradingview",
            headers={"X-Webhook-Secret": "super-secret-token-123"},
            json={"strategy": "webhook_auth_test", "trade_id": trade_id, "action": "OPEN"},
        )
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["signal_id"] == trade_id
    assert data["job_id"] is not None

    async with session_factory() as session:
        result = await session.execute(
            select(SignalJobModel).where(SignalJobModel.signal_id == trade_id)
        )
        jobs = list(result.scalars().all())
        assert len(jobs) == 1
        assert str(jobs[0].job_id) == data["job_id"]


@pytest.mark.asyncio
async def test_webhook_query_param_secret_rejected_security(
    test_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify secret passed via URL query parameter is rejected (HTTP 401) to prevent log leakage."""
    monkeypatch.setattr(
        "app.api.routes.webhooks.get_settings",
        lambda: Settings(
            webhook_auth_secret="super-secret-token-123",
            webhook_auth_enabled=True,
        ),
    )

    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/api/webhooks/tradingview?secret=super-secret-token-123",
            json={"strategy": "webhook_auth_test", "trade_id": "T-AUTH-4", "action": "OPEN"},
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
    trade_missing = f"T-DISABLED-1-{uuid4().hex[:8]}"
    trade_wrong = f"T-DISABLED-2-{uuid4().hex[:8]}"

    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://testserver"
    ) as client:
        # Request with missing secret header
        resp_missing = await client.post(
            "/api/webhooks/tradingview",
            json={"strategy": "webhook_auth_test", "trade_id": trade_missing, "action": "OPEN"},
        )
        assert resp_missing.status_code == 202

        # Request with wrong secret header
        resp_wrong = await client.post(
            "/api/webhooks/tradingview",
            headers={"X-Webhook-Secret": "wrong-secret-token"},
            json={"strategy": "webhook_auth_test", "trade_id": trade_wrong, "action": "OPEN"},
        )
        assert resp_wrong.status_code == 202

    async with session_factory() as session:
        result1 = await session.execute(
            select(SignalJobModel).where(SignalJobModel.signal_id == trade_missing)
        )
        assert list(result1.scalars().all())

        result2 = await session.execute(
            select(SignalJobModel).where(SignalJobModel.signal_id == trade_wrong)
        )
        assert list(result2.scalars().all())




@pytest.mark.asyncio
async def test_webhook_unauthorized_request_zero_db_writes(
    test_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify unauthorized requests make zero database writes to signal_jobs."""
    monkeypatch.setattr(
        "app.api.routes.webhooks.get_settings",
        lambda: Settings(webhook_auth_secret="secret123", webhook_auth_enabled=True),
    )
    session_factory: async_sessionmaker[AsyncSession] = test_app.state.session_factory

    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/api/webhooks/tradingview",
            headers={"X-Webhook-Secret": "invalid-secret"},
            json={"strategy": "webhook_auth_test", "trade_id": "T-UNAUTH-DB-0", "action": "OPEN"},
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
            json={"strategy": "webhook_auth_test", "trade_id": "T-DB-FAIL-1", "action": "OPEN"},
        )
    assert resp.status_code == 500
    assert resp.json()["detail"] == "Failed to durably persist signal job."



@pytest.mark.asyncio
async def test_webhook_duplicate_idempotency_handling(test_app: FastAPI) -> None:
    """Verify duplicate signal submission returns HTTP 202 with original job_id and creates exactly 1 job."""
    payload = {
        "strategy": "webhook_auth_test",
        "trade_id": f"T-DUP-1-{uuid4().hex[:8]}",
        "action": "OPEN",
    }
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
                .where(SignalJobModel.signal_id == payload["trade_id"])
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
            json={"strategy": "webhook_auth_test", "trade_id": "T-SEP-1", "action": "OPEN"},
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
            "strategy": "webhook_auth_test",
            "trade_id": f"T-BURST-{burst_size}-{idx}",
            "action": "OPEN",
            "direction": 1 if idx % 2 == 0 else -1,
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
                .where(SignalJobModel.strategy_id == "webhook_auth_test")
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


@pytest.mark.asyncio
async def test_webhook_fans_out_one_job_per_routed_account(test_app: FastAPI) -> None:
    from decimal import Decimal
    from uuid import uuid4

    from app.db.models.account import AccountModel
    from app.db.models.strategy import AllocationModel, StrategyModel

    session_factory: async_sessionmaker[AsyncSession] = test_app.state.session_factory
    strategy_id = f"fanout_{uuid4().hex[:8]}"
    trade_id = f"T-FANOUT-{uuid4().hex[:8]}"
    async with session_factory() as session, session.begin():
        session.add(
            StrategyModel(
                strategy_id=strategy_id,
                legs=2,
                expression="CFD",
                max_open_positions=10,
                weight_source="payload",
                enabled=True,
            )
        )
        await session.flush()
        account_ids: list[int] = []
        for i in range(2):
            acc = AccountModel(
                name=f"fanout-{i}-{uuid4().hex[:6]}",
                ibkr_account=f"DU{uuid4().hex[:8]}",
                total_margin=Decimal(100000),
                enabled=True,
            )
            session.add(acc)
            await session.flush()
            session.add(
                AllocationModel(
                    account_id=acc.id,
                    strategy_id=strategy_id,
                    alloc_pct=Decimal("0.1"),
                    enabled=True,
                    target=Decimal(500),
                    stop=Decimal(250),
                    time_limit=3600,
                    max_open_positions=10,
                )
            )
            account_ids.append(acc.id)

    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/api/webhooks/tradingview",
            json={"strategy": strategy_id, "trade_id": trade_id, "action": "OPEN"},
        )
    assert resp.status_code == 202

    async with session_factory() as session:
        jobs = list(
            (
                await session.execute(
                    select(SignalJobModel).where(SignalJobModel.signal_id == trade_id)
                )
            ).scalars().all()
        )
    assert len(jobs) == 2
    scopes = {job.account_scope for job in jobs}
    assert scopes == {str(account_ids[0]), str(account_ids[1])}
