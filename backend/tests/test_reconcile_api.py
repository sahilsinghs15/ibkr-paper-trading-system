"""Integration tests for GET /api/v1/reconcile/positions."""

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.reconcile_schemas import ReconcilePositionsResponse


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    with (
        patch(
            "app.broker.ibkr.tws_client.TWSClient.connect_and_start",
            return_value=True,
        ),
        patch("app.broker.ibkr.tws_client.TWSClient.disconnect_clean"),
        patch(
            "app.broker.ibkr.tws_client.TWSClient.is_connected",
            return_value=False,
        ),
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


def test_reconcile_positions_unknown_account_returns_empty(
    client: TestClient,
) -> None:
    response = client.get(
        "/api/v1/reconcile/positions",
        params={"ibkr_account": "UNKNOWN-RECON-ACCOUNT-XYZ"},
    )
    assert response.status_code == 200, response.text
    payload = ReconcilePositionsResponse.model_validate(response.json())
    assert payload.broker_positions == []
    assert payload.ledger_positions == []
    assert payload.diffs == []


def test_reconcile_positions_unfiltered_returns_schema(client: TestClient) -> None:
    response = client.get("/api/v1/reconcile/positions")
    assert response.status_code == 200, response.text
    ReconcilePositionsResponse.model_validate(response.json())
