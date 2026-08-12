"""API integration tests for TradingView webhook endpoint."""

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    """Startup and shutdown lifespan context for FastAPI TestClient in mock mode."""
    monkeypatch.setenv("BROKER_MODE", "mock")
    with TestClient(app) as c:
        yield c


def test_tradingview_webhook_success(client: TestClient) -> None:
    """POST /api/webhooks/tradingview accepts valid JSON payload and returns HTTP 200."""
    payload = {
        "ticker": "AAPL",
        "action": "buy",
        "price": 175.50,
        "time": "2026-08-12T15:00:00Z",
        "strategy": "FiveCandleStrategy",
    }
    response = client.post("/api/webhooks/tradingview", json=payload)
    assert response.status_code == 200
    assert response.json() == {"status": "received", "source": "tradingview"}


def test_tradingview_webhook_malformed_json(client: TestClient) -> None:
    """POST /api/webhooks/tradingview with malformed JSON body returns HTTP 400 Bad Request."""
    response = client.post(
        "/api/webhooks/tradingview",
        content="{invalid_json_body",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid or malformed JSON payload."}


def test_tradingview_webhook_non_dict_json(client: TestClient) -> None:
    """POST /api/webhooks/tradingview with non-dictionary JSON body returns HTTP 400 Bad Request."""
    response = client.post(
        "/api/webhooks/tradingview",
        json=["not", "a", "dictionary"],
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "JSON payload must be a dictionary object."}
