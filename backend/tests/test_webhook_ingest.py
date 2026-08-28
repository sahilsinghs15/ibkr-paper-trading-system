"""Tests for the standalone webhook ingest app and trading-app separation."""

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app as trading_app
from app.webhook_ingest import app as ingest_app


@pytest.fixture
def ingest_client() -> Generator[TestClient, None, None]:
    with TestClient(ingest_app) as c:
        yield c


@pytest.fixture
def trading_client() -> Generator[TestClient, None, None]:
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
        TestClient(trading_app) as c,
    ):
        yield c


def test_ingest_health_endpoint(ingest_client: TestClient) -> None:
    """Webhook ingest exposes GET /health."""
    response = ingest_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ingest_lifespan_has_no_tws_client(ingest_client: TestClient) -> None:
    """Ingest app must not wire IBKR components."""
    assert not hasattr(ingest_client.app.state, "client")
    assert not hasattr(ingest_client.app.state, "order_manager")
    assert not hasattr(ingest_client.app.state, "worker_pool")
    assert ingest_client.app.state.session_factory is not None


def test_trading_app_rejects_webhook_route(trading_client: TestClient) -> None:
    """Trading app no longer serves POST /api/webhooks/tradingview."""
    response = trading_client.post(
        "/api/webhooks/tradingview",
        json={"strategy": "model_blue", "action": "OPEN", "trade_id": "TEST-404"},
    )
    assert response.status_code == 404
