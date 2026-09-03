"""Adapter integration tests for GatewayRateLimiter."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.broker.ibkr.gateway_rate_limiter import (
    GatewayRateLimiter,
)
from app.broker.ibkr.tws_client import TWSClient
from app.instruments.models import ResolvedInstrument
from app.oms.ibkr_adapter import IBKRExecutionAdapter
from app.oms.models import OMSOrder, OMSOrderStatus
from app.rms.models import (
    ExecutionIntentMode,
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
)
from tests.ibkr_test_utils import DEFAULT_TEST_IBKR_ACCOUNT, wire_test_managed_accounts


def _pending_order(*, emergency: bool = False) -> OMSOrder:
    intent = OrderIntent(
        signal_id="T-1",
        strategy_id="model_blue",
        action=OrderAction.OPEN,
        ibkr_account=DEFAULT_TEST_IBKR_ACCOUNT,
        legs=[
            OrderLeg(
                symbol="XLE",
                side=OrderSide.BUY,
                quantity=1,
                price=Decimal(0),
                contract_month="",
            )
        ],
        intent_mode=(
            ExecutionIntentMode.EMERGENCY_FLATTEN
            if emergency
            else ExecutionIntentMode.OPEN
        ),
    )
    return OMSOrder(
        internal_order_id="ord-1",
        intent=intent,
        symbol="XLE",
        side=OrderSide.BUY,
        quantity=1.0,
        order_type="MARKET",
        status=OMSOrderStatus.PENDING,
        resolved=ResolvedInstrument(
            symbol="XLE",
            requested_instrument_type="STK",
            sec_type="STK",
            exchange="SMART",
            currency="USD",
            con_id=12345,
        ),
    )


@pytest.mark.asyncio
async def test_pacing_timeout_skips_place_order() -> None:
    limiter = GatewayRateLimiter(
        max_msg_per_sec=1.0,
        normal_msg_per_sec=1.0,
        emergency_reserve_per_sec=0.0,
        max_wait_sec=0.05,
        error100_cooldown_sec=0.0,
        max_burst=1.0,
    )
    tws = MagicMock(spec=TWSClient)
    tws.is_connected.return_value = True
    tws.next_order_id = 900
    adapter = IBKRExecutionAdapter(client=tws, rate_limiter=limiter)
    adapter.is_connected = lambda: True  # type: ignore[method-assign]
    wire_test_managed_accounts(adapter)

    first = _pending_order()
    first.internal_order_id = "ord-first"
    await adapter.submit_order(first)
    tws.placeOrder.assert_called_once()

    second = _pending_order()
    second.internal_order_id = "ord-second"
    result = await adapter.submit_order(second)
    assert result.status == OMSOrderStatus.ERROR
    assert result.error_message == "Gateway pacing timeout"
    assert tws.placeOrder.call_count == 1


@pytest.mark.asyncio
async def test_cancel_order_acquires_limiter() -> None:
    limiter = GatewayRateLimiter(
        max_msg_per_sec=5.0,
        normal_msg_per_sec=5.0,
        emergency_reserve_per_sec=0.0,
        max_wait_sec=2.0,
        error100_cooldown_sec=0.0,
    )
    tws = MagicMock(spec=TWSClient)
    tws.is_connected.return_value = True
    tws.next_order_id = 901
    tws.get_request_type.return_value = "order"
    adapter = IBKRExecutionAdapter(client=tws, rate_limiter=limiter)
    adapter.is_connected = lambda: True  # type: ignore[method-assign]
    wire_test_managed_accounts(adapter)

    order = _pending_order()
    order.internal_order_id = "ord-cancel"
    order.ibkr_order_id = 901
    adapter.adopt_order(order)

    await adapter.cancel_order(order)
    tws.cancelOrder.assert_called_once_with(901)
    assert limiter.metrics["requests_by_type"].get("cancelOrder") == 1


def test_error_100_does_not_mark_order_error() -> None:
    limiter = GatewayRateLimiter(
        max_msg_per_sec=30.0,
        normal_msg_per_sec=24.0,
        emergency_reserve_per_sec=6.0,
        max_wait_sec=2.0,
        error100_cooldown_sec=0.1,
    )
    tws = MagicMock(spec=TWSClient)
    tws.get_request_type.return_value = "order"
    adapter = IBKRExecutionAdapter(client=tws, rate_limiter=limiter)

    order = _pending_order()
    order.ibkr_order_id = 902
    adapter.adopt_order(order)
    tws._orders_by_tws_id = {}  # not used

    adapter.on_error(902, 100, "Max rate of messages per second has been exceeded")
    assert order.status == OMSOrderStatus.PENDING
    assert limiter.metrics["error100_cooldowns"] == 1
