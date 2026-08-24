"""Integration tests: Model Blue payload parsing -> sizer -> RMS -> OMS -> IBKR adapter."""

import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.broker.ibkr.tws_client import TWSClient
from app.main import app
from app.oms.ibkr_adapter import IBKRExecutionAdapter
from app.oms.oms_service import OMSService
from app.rms import RMSContext, RMSEngine
from app.rms.models import OrderSide, StrategyConfig
from app.db.session import AsyncSessionLocal
from app.services.model_blue.allocation import TemporarySettingsCommittedCapitalProvider
from app.services.order_manager import OrderManager


_COMMITTED = Decimal(25000)
_MODEL_BLUE = "model_blue"


def _qty(notional: Decimal, price: Decimal) -> float:
    return float((notional / price).quantize(Decimal(1), rounding=ROUND_DOWN))


XLE_XOP_OPEN: dict[str, Any] = {
    "market": "SMART",
    "strategy": "model_blue",
    "action": "OPEN",
    "trade_id": "MBG-AMEX:XLE-AMEX:XOP-20260817T1550",
    "direction": 1,
    "ts": "2026-08-17T15:55:00-04:00",
    "buckets": [
        {
            "underlying": "XLE",
            "legs": [
                {
                    "instrument_type": "STK",
                    "side": "BUY",
                    "weight": 0.5943,
                    "price": 62.59,
                }
            ],
        },
        {
            "underlying": "XOP",
            "legs": [
                {
                    "instrument_type": "STK",
                    "side": "SELL",
                    "weight": -0.4057,
                    "price": 183.34,
                }
            ],
        },
    ],
}

HYG_LQD_OPEN: dict[str, Any] = {
    "market": "SMART",
    "strategy": "model_blue",
    "action": "OPEN",
    "trade_id": "MBG-AMEX:HYG-AMEX:LQD-20260817T1315",
    "direction": -1,
    "ts": "2026-08-17T13:20:00-04:00",
    "buckets": [
        {
            "underlying": "HYG",
            "legs": [
                {
                    "instrument_type": "STK",
                    "side": "SELL",
                    "weight": -0.6978,
                    "price": 79.65,
                }
            ],
        },
        {
            "underlying": "LQD",
            "legs": [
                {
                    "instrument_type": "STK",
                    "side": "BUY",
                    "weight": 0.3022,
                    "price": 105.79,
                }
            ],
        },
    ],
}


@pytest.fixture
def client_with_execution() -> Generator[TestClient, None, None]:
    """TestClient with Model Blue sizing wired and a mocked TWS placeOrder."""
    tws = MagicMock(spec=TWSClient)
    tws.is_connected.return_value = True
    tws.next_order_id = 100
    tws.get_request_type.return_value = "order"

    adapter = IBKRExecutionAdapter(client=tws)
    from tests.ibkr_test_utils import fill_on_place_order

    fill_on_place_order(adapter, tws)
    oms = OMSService(adapter=adapter)
    rms_context = RMSContext(
        strategy_configs={
            _MODEL_BLUE: StrategyConfig(
                strategy_id=_MODEL_BLUE,
                max_open_positions=10,
                money_limit_per_symbol=Decimal(1_000_000),
            )
        }
    )
    order_manager = OrderManager(
        oms=oms,
        symbol=None,
        quantity=None,
        order_type="MARKET",
        price=None,
        strategy_id=_MODEL_BLUE,
        rms_engine=RMSEngine(),
        rms_context=rms_context,
        committed_capital_provider=TemporarySettingsCommittedCapitalProvider(_COMMITTED),
    )

    with (
        patch("app.broker.ibkr.tws_client.TWSClient.connect_and_start", return_value=True),
        patch("app.broker.ibkr.tws_client.TWSClient.disconnect_clean"),
        patch("app.oms.ibkr_adapter.IBKRExecutionAdapter.is_connected", return_value=True),
        TestClient(app) as c,
    ):
        app.state.order_manager = order_manager
        app.state.oms = oms
        app.state.ibkr_adapter = adapter
        yield c


def _orders(client: TestClient) -> list[Any]:
    oms: OMSService = app.state.oms
    return oms.get_all_orders()


async def _execute_signal_async(payload: dict[str, Any]) -> Any:
    order_manager: OrderManager = app.state.order_manager
    now = datetime.now(UTC)
    req_id = str(uuid.uuid4())
    domain_signal = order_manager.parse_inbound_payload(
        payload, timestamp=now, request_id=req_id, capture_data=None
    )
    return await order_manager.process_signal_execution(domain_signal)


@pytest.mark.asyncio
async def test_1_model_blue_open_direction_plus_one(client_with_execution: TestClient) -> None:
    """TEST 1: XLE/XOP OPEN +1 sizes both legs and submits two IBKR orders."""
    await _execute_signal_async(XLE_XOP_OPEN)

    orders = _orders(client_with_execution)
    assert len(orders) == 2
    by_symbol = {o.symbol: o for o in orders}
    assert set(by_symbol) == {"XLE", "XOP"}
    assert "RELIANCE" not in by_symbol

    xop_target = _COMMITTED * Decimal("0.4057") / Decimal("0.5943")
    assert by_symbol["XLE"].side == OrderSide.BUY
    assert by_symbol["XOP"].side == OrderSide.SELL
    assert by_symbol["XLE"].quantity == pytest.approx(_qty(_COMMITTED, Decimal("62.59")))
    assert by_symbol["XOP"].quantity == pytest.approx(_qty(xop_target, Decimal("183.34")))
    assert by_symbol["XLE"].quantity != 1
    assert by_symbol["XOP"].quantity != 1

    intent = orders[0].intent
    assert len(intent.legs) == 2
    assert {leg.symbol for leg in intent.legs} == {"XLE", "XOP"}
    assert orders[0].parent_signal_id == orders[1].parent_signal_id == XLE_XOP_OPEN["trade_id"]

    tws = app.state.ibkr_adapter._client
    assert tws.placeOrder.call_count == 2
    placed = [call.args[1].symbol for call in tws.placeOrder.call_args_list]
    assert placed == ["XLE", "XOP"]


@pytest.mark.asyncio
async def test_2_model_blue_open_direction_minus_one(client_with_execution: TestClient) -> None:
    """TEST 2: HYG/LQD OPEN -1 uses weight × direction, not payload side."""
    await _execute_signal_async(HYG_LQD_OPEN)

    orders = _orders(client_with_execution)
    by_symbol = {o.symbol: o for o in orders}
    assert by_symbol["HYG"].side == OrderSide.BUY
    assert by_symbol["LQD"].side == OrderSide.SELL
    assert len(orders) == 2
    tws = app.state.ibkr_adapter._client
    assert tws.placeOrder.call_count == 2


@pytest.mark.asyncio
async def test_3_model_blue_close_uses_in_memory_trade(client_with_execution: TestClient) -> None:
    """TEST 3: CLOSE with only trade_id flattens both stored legs without re-sizing."""
    await _execute_signal_async(XLE_XOP_OPEN)
    open_orders = {o.symbol: o.quantity for o in _orders(client_with_execution)}

    close_payload = {
        "market": "SMART",
        "strategy": "model_blue",
        "action": "CLOSE",
        "trade_id": XLE_XOP_OPEN["trade_id"],
        "direction": 1,
        "ts": "2026-08-17T15:20:50-04:00",
    }
    await _execute_signal_async(close_payload)

    all_orders = _orders(client_with_execution)
    assert len(all_orders) == 4
    close_orders = [o for o in all_orders if o.intent.action.value == "CLOSE"]
    assert len(close_orders) == 2
    close_by_symbol = {o.symbol: o for o in close_orders}
    assert close_by_symbol["XLE"].side == OrderSide.SELL
    assert close_by_symbol["XOP"].side == OrderSide.BUY
    assert close_by_symbol["XLE"].quantity == open_orders["XLE"]
    assert close_by_symbol["XOP"].quantity == open_orders["XOP"]
    assert "quantity" not in close_payload
    assert "buckets" not in close_payload


@pytest.mark.asyncio
async def test_4_unknown_close_rejected(client_with_execution: TestClient) -> None:
    """TEST 4: CLOSE for an unknown trade_id is rejected and creates no sell orders."""
    payload = {
        "market": "SMART",
        "strategy": "model_blue",
        "action": "CLOSE",
        "trade_id": "MBG-AMEX:EWA-AMEX:EWC-UNKNOWN",
        "direction": 1,
    }
    with pytest.raises(ValueError):
        await _execute_signal_async(payload)
    assert _orders(client_with_execution) == []


@pytest.mark.asyncio
async def test_5_invalid_open_rejected(client_with_execution: TestClient) -> None:
    """TEST 5: Invalid OPEN shapes are rejected cleanly."""
    cases = [
        {**XLE_XOP_OPEN, "buckets": []},
        {**XLE_XOP_OPEN, "buckets": [XLE_XOP_OPEN["buckets"][0]]},
        {
            **XLE_XOP_OPEN,
            "buckets": [
                {"underlying": "XLE", "legs": [{"instrument_type": "STK", "side": "BUY", "price": 62.59}]},
                XLE_XOP_OPEN["buckets"][1],
            ],
        },
        {
            **XLE_XOP_OPEN,
            "buckets": [
                {"underlying": "XLE", "legs": [{"instrument_type": "STK", "side": "BUY", "weight": 0.5}]},
                XLE_XOP_OPEN["buckets"][1],
            ],
        },
        {
            **XLE_XOP_OPEN,
            "buckets": [
                {"underlying": "", "legs": [{"instrument_type": "STK", "side": "BUY", "weight": 0.5, "price": 10}]},
                XLE_XOP_OPEN["buckets"][1],
            ],
        },
    ]
    for payload in cases:
        with pytest.raises(ValueError):
            await _execute_signal_async(payload)
        assert _orders(client_with_execution) == []


@pytest.mark.asyncio
async def test_6_no_financial_fallbacks(client_with_execution: TestClient) -> None:
    """TEST 6: Invalid Model Blue OPEN must not emit RELIANCE, quantity=1, or invented price."""
    payload = {
        "strategy": "model_blue",
        "action": "OPEN",
        "trade_id": "MBG-FALLBACK-CHECK",
        "direction": 1,
    }
    with pytest.raises(ValueError):
        await _execute_signal_async(payload)
    orders = _orders(client_with_execution)
    assert orders == []
    assert all(o.symbol != "RELIANCE" for o in orders)
    assert all(o.quantity != 1 for o in orders)


@pytest.mark.asyncio
async def test_rms_rejection_blocks_model_blue_execution() -> None:
    """RMS money-per-stock still rejects oversized Model Blue notionals."""
    tws = MagicMock(spec=TWSClient)
    tws.is_connected.return_value = True
    tws.next_order_id = 100
    adapter = IBKRExecutionAdapter(client=tws)
    oms = OMSService(adapter=adapter)
    rms_context = RMSContext(
        strategy_configs={
            _MODEL_BLUE: StrategyConfig(
                strategy_id=_MODEL_BLUE,
                max_open_positions=10,
                money_limit_per_symbol=Decimal("5.00"),
            )
        }
    )
    order_manager = OrderManager(
        oms=oms,
        quantity=None,
        symbol=None,
        strategy_id=_MODEL_BLUE,
        rms_engine=RMSEngine(),
        rms_context=rms_context,
        committed_capital_provider=TemporarySettingsCommittedCapitalProvider(_COMMITTED),
        session_factory=None,
    )



    now = datetime.now(UTC)
    req_id = str(uuid.uuid4())
    payload = {**XLE_XOP_OPEN, "trade_id": f"MBG-RMS-REJECT-{uuid.uuid4().hex[:6]}"}
    domain_signal = order_manager.parse_inbound_payload(
        payload, timestamp=now, request_id=req_id, capture_data=None
    )
    with pytest.raises(ValueError):
        await order_manager.process_signal_execution(domain_signal)
    assert oms.get_all_orders() == []
    assert tws.placeOrder.call_count == 0




