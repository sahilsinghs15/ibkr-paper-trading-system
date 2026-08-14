"""Integration tests for application dependency injection and component wiring."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.broker.ibkr.tws_client import TWSClient
from app.main import create_app
from app.oms import IBKRExecutionAdapter, OMSService
from app.services.order_manager import OrderManager


@pytest.fixture
def app_client():
    """Test client configured for FastAPI app with mocked socket connection."""
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


def test_app_lifespan_wiring(app_client):
    """Verify application lifespan injects TWSClient, IBKRExecutionAdapter, OMSService, and OrderManager."""
    app = app_client.app

    # Verify app state components
    assert isinstance(app.state.client, TWSClient)
    assert isinstance(app.state.ibkr_adapter, IBKRExecutionAdapter)
    assert isinstance(app.state.oms, OMSService)
    assert isinstance(app.state.order_manager, OrderManager)


def test_orders_endpoint_routes_to_oms(app_client):
    """Verify orders endpoint queries the injected OMSService."""
    response = app_client.get("/api/v1/orders")
    assert response.status_code == 200
    assert isinstance(response.json(), list)
