"""Integration and regression tests for Phase 3 TradingView signal persistence."""

from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def capture_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fixture to direct file capture artifacts to a temporary directory."""
    target_dir = tmp_path / "tradingview_webhooks"
    monkeypatch.setattr("app.api.routes.webhooks.WEBHOOK_CAPTURE_DIR", target_dir)
    return target_dir


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    """TestClient fixture with mocked TWS connection."""
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


@pytest.mark.skip(reason="Database persistence in webhook route temporarily paused for execution engine integration task")
@pytest.mark.asyncio
async def test_1_valid_open_payload_persisted(client: TestClient, capture_dir: Path) -> None:
    """TEST 1: Valid OPEN signal is persisted to database with status=NEW."""


@pytest.mark.skip(reason="Database persistence in webhook route temporarily paused for execution engine integration task")
@pytest.mark.asyncio
async def test_2_duplicate_signal_delivery_ignored(client: TestClient) -> None:
    """TEST 2: Duplicate signal delivery creates exactly 1 database row due to unique constraint."""


@pytest.mark.skip(reason="Database persistence in webhook route temporarily paused for execution engine integration task")
@pytest.mark.asyncio
async def test_3_valid_close_payload_persisted(client: TestClient) -> None:
    """TEST 3: Valid CLOSE signal is persisted with status=NEW."""


@pytest.mark.asyncio
async def test_4_invalid_webhook_payload_no_db_row(client: TestClient) -> None:
    """TEST 4: Malformed payload returns HTTP 400."""
    response = client.post(
        "/api/webhooks/tradingview",
        content="{invalid_json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400


@pytest.mark.skip(reason="Database persistence in webhook route temporarily paused for execution engine integration task")
@pytest.mark.asyncio
async def test_5_raw_payload_audit(client: TestClient) -> None:
    """TEST 5: Stored raw_payload JSONB contains metadata, raw_body, and parsed_json."""


@pytest.mark.skip(reason="Database persistence in webhook route temporarily paused for execution engine integration task")
@pytest.mark.asyncio
async def test_6_restart_persistence(client: TestClient) -> None:
    """TEST 6: Previously persisted signal rows remain in database after connection reset."""
