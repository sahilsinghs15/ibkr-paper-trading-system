"""API integration tests for health, market-data ingestion, orders, positions,

and account lifespan lifecycles.
"""

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.broker import BrokerStatus


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    """Startup and shutdown lifespan context for FastAPI client."""
    monkeypatch.setenv("BROKER_MODE", "mock")
    with TestClient(app) as c:
        yield c


def test_health_endpoint(client: TestClient) -> None:
    """GET /health health-check behaves cleanly."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_invalid_market_data_schema(client: TestClient) -> None:
    """Invalid payload formats and naive datetimes are rejected with 400 Bad

    Request.
    """
    response = client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:00:00",  # naive timestamp
            "price": "100.00",
            "volume": 10,
        },
    )
    assert response.status_code == 400
    assert "timezone-aware" in response.json()["detail"]


def test_incomplete_candle_market_data(client: TestClient) -> None:
    """Submitting a tick that does not close a candle yields

    candle_completed = False.
    """
    response = client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:00:00Z",
            "price": "100.00",
            "volume": 10,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["candle_completed"] is False
    assert data["signal"] is None
    assert data["order"] is None


def test_five_bullish_candles_triggers_buy_order(client: TestClient) -> None:
    """Ingesting events completing 5 bullish candles generates a BUY order."""
    # Feed events to build 5 completed bullish candles.
    # Candle 1: starts 10:00, updates 10:04, completes 10:05 (open=100, close=101)
    client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:00:00Z",
            "price": "100.00",
            "volume": 10,
        },
    )
    client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:04:00Z",
            "price": "101.00",
            "volume": 10,
        },
    )

    res = client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:05:00Z",
            "price": "101.00",
            "volume": 10,
        },
    )
    assert res.json()["candle_completed"] is True
    assert res.json()["signal"]["signal_type"] == "HOLD"

    # Candle 2: starts 10:05, updates 10:09, completes 10:10 (open=101, close=102)
    client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:09:00Z",
            "price": "102.00",
            "volume": 10,
        },
    )
    res = client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:10:00Z",
            "price": "102.00",
            "volume": 10,
        },
    )
    assert res.json()["candle_completed"] is True

    # Candle 3: starts 10:10, updates 10:14, completes 10:15 (open=102, close=103)
    client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:14:00Z",
            "price": "103.00",
            "volume": 10,
        },
    )
    res = client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:15:00Z",
            "price": "103.00",
            "volume": 10,
        },
    )
    assert res.json()["candle_completed"] is True

    # Candle 4: starts 10:15, updates 10:19, completes 10:20 (open=103, close=104)
    client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:19:00Z",
            "price": "104.00",
            "volume": 10,
        },
    )
    res = client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:20:00Z",
            "price": "104.00",
            "volume": 10,
        },
    )
    assert res.json()["candle_completed"] is True

    # Candle 5: starts 10:20, updates 10:24, completes 10:25 (open=104, close=105)
    client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:24:00Z",
            "price": "105.00",
            "volume": 10,
        },
    )
    res = client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:25:00Z",
            "price": "105.00",
            "volume": 10,
        },
    )
    assert res.json()["candle_completed"] is True
    assert res.json()["signal"]["signal_type"] == "BUY"
    assert res.json()["order"]["side"] == "BUY"
    assert res.json()["order"]["symbol"] == "RELIANCE"

    # Verify orders retrieval through broker API wrapper
    orders_res = client.get("/api/v1/orders")
    assert orders_res.status_code == 200
    orders = orders_res.json()
    assert len(orders) == 1
    assert orders[0]["side"] == "BUY"
    assert orders[0]["symbol"] == "RELIANCE"


def test_five_bearish_candles_triggers_sell_order(client: TestClient) -> None:
    """Ingesting events completing 5 bearish candles generates a SELL order."""
    # Feed events to build 5 completed bearish candles.
    # Candle 1: starts 10:00, updates 10:04, completes 10:05 (open=100, close=99)
    client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:00:00Z",
            "price": "100.00",
            "volume": 10,
        },
    )
    client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:04:00Z",
            "price": "99.00",
            "volume": 10,
        },
    )

    res = client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:05:00Z",
            "price": "99.00",
            "volume": 10,
        },
    )
    assert res.json()["candle_completed"] is True

    # Candle 2: starts 10:05, updates 10:09, completes 10:10 (open=99, close=98)
    client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:09:00Z",
            "price": "98.00",
            "volume": 10,
        },
    )
    res = client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:10:00Z",
            "price": "98.00",
            "volume": 10,
        },
    )
    assert res.json()["candle_completed"] is True

    # Candle 3: starts 10:10, updates 10:14, completes 10:15 (open=98, close=97)
    client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:14:00Z",
            "price": "97.00",
            "volume": 10,
        },
    )
    res = client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:15:00Z",
            "price": "97.00",
            "volume": 10,
        },
    )
    assert res.json()["candle_completed"] is True

    # Candle 4: starts 10:15, updates 10:19, completes 10:20 (open=97, close=96)
    client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:19:00Z",
            "price": "96.00",
            "volume": 10,
        },
    )
    res = client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:20:00Z",
            "price": "96.00",
            "volume": 10,
        },
    )
    assert res.json()["candle_completed"] is True

    # Candle 5: starts 10:20, updates 10:24, completes 10:25 (open=96, close=95)
    client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:24:00Z",
            "price": "95.00",
            "volume": 10,
        },
    )
    res = client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:25:00Z",
            "price": "95.00",
            "volume": 10,
        },
    )
    assert res.json()["candle_completed"] is True
    assert res.json()["signal"]["signal_type"] == "SELL"
    assert res.json()["order"]["side"] == "SELL"


def test_mixed_candles_triggers_hold(client: TestClient) -> None:
    """Ingesting a mixed pattern of bullish and bearish candles yields HOLD."""
    # Candle 1: Bullish (100 -> 101)
    client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:00:00Z",
            "price": "100.00",
            "volume": 10,
        },
    )
    client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:04:00Z",
            "price": "101.00",
            "volume": 10,
        },
    )

    # Candle 2: Bearish (101 -> 100)
    client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:05:00Z",
            "price": "101.00",
            "volume": 10,
        },
    )
    client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:09:00Z",
            "price": "100.00",
            "volume": 10,
        },
    )

    # Candle 3: Bullish (100 -> 101)
    client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:10:00Z",
            "price": "100.00",
            "volume": 10,
        },
    )
    client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:14:00Z",
            "price": "101.00",
            "volume": 10,
        },
    )

    # Candle 4: Bearish (101 -> 100)
    client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:15:00Z",
            "price": "101.00",
            "volume": 10,
        },
    )
    client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:19:00Z",
            "price": "100.00",
            "volume": 10,
        },
    )

    # Candle 5: Bullish (100 -> 101)
    client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:20:00Z",
            "price": "100.00",
            "volume": 10,
        },
    )
    client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:24:00Z",
            "price": "101.00",
            "volume": 10,
        },
    )

    res = client.post(
        "/api/v1/market-data",
        json={
            "timestamp": "2025-06-15T10:25:00Z",
            "price": "101.00",
            "volume": 10,
        },
    )
    assert res.json()["candle_completed"] is True
    assert res.json()["signal"]["signal_type"] == "HOLD"
    assert res.json()["order"] is None


def test_get_positions(client: TestClient) -> None:
    """GET /api/v1/positions executes cleanly."""
    res = client.get("/api/v1/positions")
    assert res.status_code == 200
    assert isinstance(res.json(), list)


def test_get_margin(client: TestClient) -> None:
    """GET /api/v1/margin retrieves equity context."""
    res = client.get("/api/v1/margin")
    assert res.status_code == 200
    data = res.json()
    assert "equity" in data
    assert "available_funds" in data
    assert "buying_power" in data


def test_lifecycle_startup_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lifespan manages initialization and disconnect."""
    from fastapi import FastAPI

    monkeypatch.setenv("BROKER_MODE", "mock")
    broker_ref = None
    with TestClient(app) as c:
        # State initialized once on startup and stored
        fastapi_app = c.app
        assert isinstance(fastapi_app, FastAPI)
        broker_ref = fastapi_app.state.broker
        assert broker_ref.status == BrokerStatus.CONNECTED

    # State disconnected cleanly on shutdown
    assert broker_ref.status == BrokerStatus.DISCONNECTED
