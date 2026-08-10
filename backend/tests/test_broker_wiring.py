"""Integration tests for Broker dependency injection and wiring."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.broker.ibkr.ibkr_broker import IBKRBroker
from app.broker.mock_broker import MockBroker
from app.main import create_app


@pytest.fixture
def mock_mode_client(monkeypatch):
    """Test client configured for mock broker mode."""
    monkeypatch.setenv("BROKER_MODE", "mock")
    app = create_app()
    with TestClient(app) as client:
        yield client


@pytest.fixture
def ibkr_mode_client(monkeypatch):
    """Test client configured for IBKR broker mode with mocked TWS."""
    monkeypatch.setenv("BROKER_MODE", "ibkr")

    # Mock TWS client connection to avoid needing real TWS during tests
    with (
        patch(
            "app.broker.ibkr.tws_client.TWSClient.connect_and_start",
            return_value=True,
        ),
        patch("app.broker.ibkr.tws_client.TWSClient.disconnect_clean"),
    ):
        app = create_app()
        with TestClient(app) as client:
            yield client


def test_mock_mode_wiring(mock_mode_client):
    """Verify mock mode injects MockBroker and sets correct status."""
    app = mock_mode_client.app

    # Verify app state
    assert app.state.broker_mode == "mock"
    assert isinstance(app.state.broker, MockBroker)
    assert getattr(app.state, "market_data_adapter", None) is None

    # Verify status endpoint
    response = mock_mode_client.get("/api/v1/broker/status")
    assert response.status_code == 200
    data = response.json()
    assert data["broker_mode"] == "mock"
    assert data["broker_type"] == "MockBroker"
    assert data["connected"] is True  # MockBroker is always connected after login

    # Verify subscribe endpoint is disabled in mock mode
    response = mock_mode_client.post("/api/v1/market-data/subscribe")
    assert response.status_code == 400
    assert "only available in IBKR mode" in response.json()["detail"]


def test_ibkr_mode_wiring(ibkr_mode_client):
    """Verify IBKR mode injects IBKRBroker and market data adapter."""
    app = ibkr_mode_client.app

    # Verify app state
    assert app.state.broker_mode == "ibkr"
    assert isinstance(app.state.broker, IBKRBroker)
    assert getattr(app.state, "market_data_adapter", None) is not None

    # Verify status endpoint
    response = ibkr_mode_client.get("/api/v1/broker/status")
    assert response.status_code == 200
    data = response.json()
    assert data["broker_mode"] == "ibkr"
    assert data["broker_type"] == "IBKRBroker"


def test_order_endpoints_use_injected_broker_mock(mock_mode_client):
    """Verify order endpoints route to the injected MockBroker."""
    with patch.object(
        MockBroker,
        "get_order_book",
        wraps=mock_mode_client.app.state.broker.get_order_book,
    ) as mock_get:
        response = mock_mode_client.get("/api/v1/orders")
        assert response.status_code == 200
        mock_get.assert_called_once()

    response = mock_mode_client.post(
        "/api/v1/orders",
        json={
            "symbol": "AAPL",
            "side": "BUY",
            "quantity": 10,
            "order_type": "MARKET",
        },
    )
    assert response.status_code == 200
    assert response.json()["symbol"] == "AAPL"


def test_order_endpoints_use_injected_broker_ibkr(ibkr_mode_client):
    """Verify order endpoints route to the injected IBKRBroker."""
    with patch.object(IBKRBroker, "get_order_book", return_value=[]) as mock_get:
        response = ibkr_mode_client.get("/api/v1/orders")
        assert response.status_code == 200
        mock_get.assert_called_once()
