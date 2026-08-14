"""API integration tests for health, webhooks, orders, and lifespan lifecycles."""

from collections.abc import Generator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    """Startup and shutdown lifespan context for FastAPI client with mocked TWS connection."""
    with (
        patch(
            "app.broker.ibkr.tws_client.TWSClient.connect_and_start",
            return_value=True,
        ),
        patch("app.broker.ibkr.tws_client.TWSClient.disconnect_clean"),
        patch(
            "app.broker.ibkr.tws_client.TWSClient.is_connected",
            return_value=True,
        ),
        TestClient(app) as c,
    ):
        yield c


def test_health_endpoint(client: TestClient) -> None:
    """GET /health health-check behaves cleanly."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_get_orders(client: TestClient) -> None:
    """GET /api/v1/orders returns order list."""
    res = client.get("/api/v1/orders")
    assert res.status_code == 200
    assert isinstance(res.json(), list)


def test_get_order_by_id_not_found(client: TestClient) -> None:
    """GET /api/v1/orders/{order_id} returns 404 for unknown order ID."""
    res = client.get("/api/v1/orders/UNKNOWN-ID")
    assert res.status_code == 404


def test_cancel_order_not_found(client: TestClient) -> None:
    """DELETE /api/v1/orders/{order_id} returns 404 for unknown order ID."""
    res = client.delete("/api/v1/orders/UNKNOWN-ID")
    assert res.status_code == 404


def test_lifecycle_startup_shutdown() -> None:
    """Lifespan manages initialization and disconnect."""
    from fastapi import FastAPI

    with (
        patch(
            "app.broker.ibkr.tws_client.TWSClient.connect_and_start",
            return_value=True,
        ),
        patch("app.broker.ibkr.tws_client.TWSClient.disconnect_clean"),
        TestClient(app) as c,
    ):
        fastapi_app = c.app
        assert isinstance(fastapi_app, FastAPI)
        oms_ref = fastapi_app.state.oms
        assert oms_ref is not None
