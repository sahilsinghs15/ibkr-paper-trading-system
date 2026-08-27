"""API integration tests for TradingView webhook endpoint."""

import csv
import json
import uuid
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api.routes.webhooks import INCOMING_SIGNALS_CSV_NAME
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
            "app.services.order_manager.OrderManager.hydrate_live_pnl",
            new_callable=AsyncMock,
        ),
        TestClient(app) as c,
    ):
        yield c


def test_tradingview_webhook_success_and_persists_artifact(
    client: TestClient, capture_dir: Path
) -> None:
    """POST /api/webhooks/tradingview accepts valid JSON payload and returns HTTP 202 Accepted."""
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
    assert response.json()["status"] == "accepted"
    assert response.json()["signal_id"] is not None

    csv_path = capture_dir / INCOMING_SIGNALS_CSV_NAME
    assert csv_path.is_file()
    with csv_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["signal_id"] == response.json()["signal_id"]
    assert rows[0]["job_id"] == response.json()["job_id"]
    assert rows[0]["duplicate"] == "false"
    assert rows[0]["strategy"] == "FiveCandleStrategy"
    assert rows[0]["action"] == "buy"
    assert json.loads(rows[0]["raw_json"]) == payload



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

    # Ensure no capture artifact or CSV row was persisted
    if capture_dir.exists():
        captured_files = list(capture_dir.glob("*.json"))
        assert len(captured_files) == 0
        assert not (capture_dir / INCOMING_SIGNALS_CSV_NAME).exists()


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

    # Ensure no capture artifact or CSV row was persisted
    if capture_dir.exists():
        captured_files = list(capture_dir.glob("*.json"))
        assert len(captured_files) == 0
        assert not (capture_dir / INCOMING_SIGNALS_CSV_NAME).exists()


def test_tradingview_webhook_appends_all_signals_to_csv(
    client: TestClient, capture_dir: Path
) -> None:
    """Every accepted webhook, including duplicates, is appended to the temporary CSV."""
    trade_id = f"MBG-CSV-{uuid.uuid4().hex[:12].upper()}"
    payload = {
        "market": "SMART",
        "strategy": "model_blue",
        "action": "OPEN",
        "trade_id": trade_id,
        "direction": -1,
        "ts": "2026-08-26T12:00:00-04:00",
        "buckets": [
            {
                "underlying": "EWP",
                "legs": [
                    {
                        "instrument_type": "STK",
                        "side": "SELL",
                        "weight": -0.4841,
                        "price": 63.14,
                    }
                ],
            },
            {
                "underlying": "EWU",
                "legs": [
                    {
                        "instrument_type": "STK",
                        "side": "BUY",
                        "weight": 0.5159,
                        "price": 49.1,
                    }
                ],
            },
        ],
    }
    first = client.post("/api/webhooks/tradingview", json=payload)
    second = client.post("/api/webhooks/tradingview", json=payload)
    assert first.status_code == 202
    assert second.status_code == 202

    csv_path = capture_dir / INCOMING_SIGNALS_CSV_NAME
    with csv_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2
    assert rows[0]["duplicate"] == "false"
    assert rows[1]["duplicate"] == "true"
    assert rows[0]["trade_id"] == payload["trade_id"]
    assert rows[0]["leg_a_symbol"] == "EWP"
    assert rows[0]["leg_a_side"] == "SELL"
    assert rows[0]["leg_a_weight"] == "-0.4841"
    assert rows[0]["leg_a_price"] == "63.14"
    assert rows[0]["leg_b_symbol"] == "EWU"
    assert rows[0]["leg_b_side"] == "BUY"
    assert json.loads(rows[0]["raw_json"])["trade_id"] == payload["trade_id"]

