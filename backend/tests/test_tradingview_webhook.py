"""API integration tests for TradingView webhook endpoint."""

import json
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def capture_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Fixture to direct webhook JSON persistence to a temporary directory."""
    target_dir = tmp_path / "tradingview_webhooks"
    monkeypatch.setattr("app.api.routes.webhooks.WEBHOOK_CAPTURE_DIR", target_dir)
    return target_dir



@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    """Startup and shutdown lifespan context for FastAPI TestClient with mocked TWS connection."""
    with (
        patch("app.broker.ibkr.tws_client.TWSClient.connect_and_start", return_value=True),
        patch("app.broker.ibkr.tws_client.TWSClient.disconnect_clean"),
        patch("app.oms.ibkr_adapter.IBKRExecutionAdapter.is_connected", return_value=True),
        patch("app.oms.ibkr_adapter.IBKRExecutionAdapter.submit_order", side_effect=lambda o: o),
        TestClient(app) as c,
    ):
        yield c


def test_tradingview_webhook_success_and_persists_artifact(
    client: TestClient, capture_dir: Path
) -> None:
    """POST /api/webhooks/tradingview accepts valid JSON payload, returns HTTP 200, and creates capture file."""
    payload = {
        "ticker": "AAPL",
        "action": "buy",
        "price": 175.50,
        "time": "2026-08-12T15:00:00Z",
        "strategy": "FiveCandleStrategy",
    }
    response = client.post("/api/webhooks/tradingview", json=payload)
    assert response.status_code in (200, 202)
    assert response.json()["source"] == "tradingview"
    assert response.json()["status"] in {"received", "accepted", "rejected_by_rms"}

    # Verify persistent capture artifact
    captured_files = list(capture_dir.glob("webhook_*.json"))
    assert len(captured_files) == 1

    capture_file = captured_files[0]
    data = json.loads(capture_file.read_text(encoding="utf-8"))

    assert "metadata" in data
    assert "request_id" in data["metadata"]
    assert "received_at" in data["metadata"]
    assert data["parsed_json"] == payload
    assert json.loads(data["raw_body"]) == payload


def test_tradingview_webhook_malformed_json_no_artifact(
    client: TestClient, capture_dir: Path
) -> None:
    """POST /api/webhooks/tradingview with malformed JSON body returns HTTP 400 Bad Request and creates no file."""
    raw_invalid_body = "{invalid_json_body"
    response = client.post(
        "/api/webhooks/tradingview",
        content=raw_invalid_body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid or malformed JSON payload."}

    # Ensure no capture artifact was persisted
    if capture_dir.exists():
        captured_files = list(capture_dir.glob("*.json"))
        assert len(captured_files) == 0


def test_tradingview_webhook_non_dict_json_no_artifact(
    client: TestClient, capture_dir: Path
) -> None:
    """POST /api/webhooks/tradingview with non-dictionary JSON body returns HTTP 400 Bad Request and creates no file."""
    response = client.post(
        "/api/webhooks/tradingview",
        json=["not", "a", "dictionary"],
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "JSON payload must be a dictionary object."}

    # Ensure no capture artifact was persisted
    if capture_dir.exists():
        captured_files = list(capture_dir.glob("*.json"))
        assert len(captured_files) == 0

