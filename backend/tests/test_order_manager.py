"""Tests for OrderManager facade."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.signal import Signal, SignalType
from app.oms.models import ExecutionResult, OMSOrder, OrderStatus
from app.oms.oms_service import OMSService
from app.rms.models import OrderIntent, RMSContext, StrategyConfig
from app.rms.models import OrderSide as RMSOrderSide
from app.services.order_manager import OrderManager

_SYMBOL = "RELIANCE"
_QUANTITY = 1
_ORDER_TYPE = "MARKET"


def _signal(signal_type: SignalType) -> Signal:
    """Create a deterministic Signal."""
    return Signal(
        signal_type=signal_type,
        timestamp=datetime(2025, 6, 15, 10, 5, 0, tzinfo=UTC),
        reason="test signal",
    )


def _make_oms_stub() -> AsyncMock:
    """Create an OMSService stub with an async ``submit_intent`` mock."""
    stub = AsyncMock(spec=OMSService)
    dummy_intent = OrderIntent(
        signal_id="SIG-001",
        strategy_id="MODEL_BLUE",
        action=None,
        legs=[],
        timestamp=datetime.now(UTC),
    )
    dummy_order = OMSOrder(
        internal_order_id="ORD-001",
        intent=dummy_intent,
        symbol=_SYMBOL,
        side=RMSOrderSide.BUY,
        quantity=_QUANTITY,
        status=OrderStatus.SUBMITTED,
    )
    stub.submit_intent.return_value = ExecutionResult(
        order=dummy_order,
        rms_result=AsyncMock(outcome=AsyncMock(value="PASS")),
        success=True,
    )
    return stub


@pytest.mark.asyncio
async def test_buy_signal_submits_intent_to_oms() -> None:
    oms = _make_oms_stub()
    mgr = OrderManager(
        oms=oms,
        symbol=_SYMBOL,
        quantity=_QUANTITY,
        order_type=_ORDER_TYPE,
        rms_context=RMSContext(
            strategy_configs={
                "default_strategy": StrategyConfig(
                    strategy_id="default_strategy",
                    max_open_positions=100,
                    money_limit_per_symbol=Decimal("1000000"),
                )
            }
        ),
    )
    res = await mgr.process_signal(_signal(SignalType.BUY))
    assert res is not None
    oms.submit_intent.assert_called_once()


@pytest.mark.asyncio
async def test_sell_signal_submits_intent_to_oms() -> None:
    oms = _make_oms_stub()
    mgr = OrderManager(
        oms=oms,
        symbol=_SYMBOL,
        quantity=_QUANTITY,
        order_type=_ORDER_TYPE,
        rms_context=RMSContext(
            strategy_configs={
                "default_strategy": StrategyConfig(
                    strategy_id="default_strategy",
                    max_open_positions=100,
                    money_limit_per_symbol=Decimal("1000000"),
                )
            }
        ),
    )
    res = await mgr.process_signal(_signal(SignalType.SELL))
    assert res is not None
    oms.submit_intent.assert_called_once()


@pytest.mark.asyncio
async def test_hold_signal_returns_none() -> None:
    oms = _make_oms_stub()
    mgr = OrderManager(
        oms=oms,
        symbol=_SYMBOL,
        quantity=_QUANTITY,
        order_type=_ORDER_TYPE,
        rms_context=RMSContext(
            strategy_configs={
                "default_strategy": StrategyConfig(
                    strategy_id="default_strategy",
                    max_open_positions=100,
                    money_limit_per_symbol=Decimal("1000000"),
                )
            }
        ),
    )
    res = await mgr.process_signal(_signal(SignalType.HOLD))
    assert res is None
    oms.submit_intent.assert_not_called()


def test_tws_client_returns_adapter_client_from_oms_service() -> None:
    """CFD discovery must read oms._adapter._client, not the nonexistent oms.adapter."""
    sentinel = MagicMock(name="tws_client")
    adapter = MagicMock()
    adapter._client = sentinel
    oms = OMSService(adapter=adapter)
    assert getattr(oms, "adapter", None) is None

    mgr = OrderManager(oms=oms)
    assert mgr._tws_client() is sentinel


def test_tws_client_returns_none_when_oms_is_none() -> None:
    mgr = OrderManager(oms=None)
    assert mgr._tws_client() is None
