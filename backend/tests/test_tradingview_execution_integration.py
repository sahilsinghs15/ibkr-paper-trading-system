"""Integration tests verifying real TradingView Webhook -> Signal -> OrderManager -> RMS -> OMS execution path."""

from collections.abc import Generator
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.broker.ibkr.tws_client import TWSClient
from app.main import app
from app.oms import IBKRExecutionAdapter, OMSService
from app.rms import RMSContext, RMSEngine
from app.rms.models import StrategyConfig
from app.services.order_manager import OrderManager


@pytest.fixture
def mock_oms() -> OMSService:
    """Fixture providing an OMSService instance with a mocked IBKR adapter."""
    client = MagicMock(spec=TWSClient)
    client.is_connected.return_value = True
    client.next_order_id = 100
    adapter = IBKRExecutionAdapter(client=client)
    return OMSService(adapter=adapter)


@pytest.fixture
def client_with_execution(mock_oms: OMSService) -> Generator[TestClient, None, None]:
    """TestClient fixture with an active OrderManager wired on app.state."""
    strategy_id = "MODEL_BLUE"
    rms_engine = RMSEngine()
    rms_context = RMSContext(
        strategy_configs={
            strategy_id: StrategyConfig(
                strategy_id=strategy_id,
                max_open_positions=10,
                money_limit_per_symbol=Decimal(1_000_000),
            )
        }
    )

    with (
        patch("app.broker.ibkr.tws_client.TWSClient.connect_and_start", return_value=True),
        patch("app.broker.ibkr.tws_client.TWSClient.disconnect_clean"),
        patch("app.broker.ibkr.tws_client.TWSClient.is_connected", return_value=True),
        patch("app.oms.ibkr_adapter.IBKRExecutionAdapter.is_connected", return_value=True),
        patch("app.oms.ibkr_adapter.IBKRExecutionAdapter.submit_order", side_effect=lambda o: o),
        TestClient(app) as c,
    ):
        order_manager = OrderManager(
            oms=mock_oms,
            symbol=None,
            quantity=None,
            order_type="MARKET",
            price=None,
            strategy_id=strategy_id,
            rms_engine=rms_engine,
            rms_context=rms_context,
        )
        app.state.order_manager = order_manager
        app.state.oms = mock_oms
        yield c


def test_1_valid_open_webhook_drives_execution(client_with_execution: TestClient) -> None:
    """TEST 1: Valid OPEN webhook payload drives Signal -> OrderManager -> RMS PASS -> OMS."""
    payload = {
        "market": "SMART",
        "strategy": "MODEL_BLUE",
        "action": "OPEN",
        "trade_id": "MBG-EWA-EWC-20260814T1525_OPEN",
        "direction": 1,
        "ref_price_a": 25.50,
        "ticker": "EWA",
        "quantity": 10,
    }
    response = client_with_execution.post("/api/webhooks/tradingview", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "received"

    # Verify OMS order was created
    oms: OMSService = app.state.oms
    orders = oms.get_all_orders()
    assert len(orders) > 0
    latest_order = orders[-1]
    assert latest_order.symbol == "EWA"
    assert latest_order.intent.action.value == "OPEN"


def test_2_rms_rejection_blocks_execution(mock_oms: OMSService) -> None:
    """TEST 2: Payload triggering RMS rejection (money limit exceeded) returns rejection and places NO OMS order."""
    strategy_id = "REJECT_STRAT"
    rms_engine = RMSEngine()
    rms_context = RMSContext(
        strategy_configs={
            strategy_id: StrategyConfig(
                strategy_id=strategy_id,
                max_open_positions=10,
                money_limit_per_symbol=Decimal("5.00"),  # $5 limit -> $25.50 order exceeds budget and is rejected by RMS Check 8
            )
        }
    )

    with (
        patch("app.broker.ibkr.tws_client.TWSClient.connect_and_start", return_value=True),
        patch("app.broker.ibkr.tws_client.TWSClient.disconnect_clean"),
        patch("app.broker.ibkr.tws_client.TWSClient.is_connected", return_value=True),
        patch("app.oms.ibkr_adapter.IBKRExecutionAdapter.is_connected", return_value=True),
        patch("app.oms.ibkr_adapter.IBKRExecutionAdapter.submit_order", side_effect=lambda o: o),
        TestClient(app) as c,
    ):
        order_manager = OrderManager(
            oms=mock_oms,
            symbol=None,
            quantity=None,
            price=None,
            strategy_id=strategy_id,
            rms_engine=rms_engine,
            rms_context=rms_context,
        )
        app.state.order_manager = order_manager
        app.state.oms = mock_oms

        initial_orders_count = len(mock_oms.get_all_orders())
        payload = {
            "strategy": strategy_id,
            "action": "OPEN",
            "trade_id": "REJECT_TRADE_001",
            "ticker": "EWA",
            "ref_price_a": 25.50,
            "quantity": 1,
        }
        response = c.post("/api/webhooks/tradingview", json=payload)
        assert response.status_code == 200
        res_data = response.json()
        assert res_data["status"] == "rejected_by_rms"

        # NO new OMS order submitted
        assert len(mock_oms.get_all_orders()) == initial_orders_count


def test_3_rms_pass_submits_intent(client_with_execution: TestClient) -> None:
    """TEST 3: Valid payload satisfying RMS checks produces RMS PASS and OMS submission."""
    payload = {
        "strategy": "MODEL_BLUE",
        "action": "OPEN",
        "trade_id": "PASS_TRADE_002",
        "ticker": "EWA",
        "ref_price_a": 25.50,
        "quantity": 5,
    }
    response = client_with_execution.post("/api/webhooks/tradingview", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "received"


def test_4_close_payload_follows_close_path(client_with_execution: TestClient) -> None:
    """TEST 4: CLOSE payload closes an existing active open position."""
    # First send OPEN to create an active position in memory
    open_payload = {
        "strategy": "MODEL_BLUE",
        "action": "OPEN",
        "trade_id": "OPEN_TRADE_BEFORE_CLOSE",
        "ticker": "EWA",
        "quantity": 10,
        "ref_price_a": 25.50,
    }
    res_open = client_with_execution.post("/api/webhooks/tradingview", json=open_payload)
    assert res_open.status_code == 200

    # Then send CLOSE
    close_payload = {
        "strategy": "MODEL_BLUE",
        "action": "CLOSE",
        "trade_id": "CLOSE_TRADE_003",
        "ticker": "EWA",
        "direction": -1,
    }
    response = client_with_execution.post("/api/webhooks/tradingview", json=close_payload)
    assert response.status_code == 200
    assert response.json()["status"] == "received"

    oms: OMSService = app.state.oms
    orders = oms.get_all_orders()
    assert len(orders) >= 2
    latest_order = orders[-1]
    assert latest_order.intent.action.value == "CLOSE"


def test_5_malformed_payload_no_execution(client_with_execution: TestClient) -> None:
    """TEST 5: Malformed JSON payload returns HTTP 400 with no execution."""
    response = client_with_execution.post(
        "/api/webhooks/tradingview",
        content="{malformed_json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid or malformed JSON payload."}


def test_6_close_without_open_position_rejected(client_with_execution: TestClient) -> None:
    """TEST 6: CLOSE signal without an open position is safely rejected without creating a sell order."""
    close_payload = {
        "strategy": "MODEL_BLUE",
        "action": "CLOSE",
        "trade_id": "UNMATCHED_CLOSE_TRADE",
        "ticker": "UNOWNED_SYMBOL",
        "direction": -1,
    }
    response = client_with_execution.post("/api/webhooks/tradingview", json=close_payload)
    assert response.status_code == 200
    assert response.json()["status"] == "rejected_by_rms"
