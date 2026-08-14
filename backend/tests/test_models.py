"""Tests for active domain models."""

from datetime import UTC, datetime
from decimal import Decimal

from app.models.signal import Signal, SignalType
from app.oms.models import OMSOrder, OrderStatus
from app.rms.models import OrderIntent
from app.rms.models import OrderSide as RMSOrderSide


class TestSignal:
    """Tests for Signal domain model."""

    def test_signal_construction(self) -> None:
        signal = Signal(
            signal_type=SignalType.BUY,
            timestamp=datetime(2025, 1, 1, 10, 5, tzinfo=UTC),
            reason="Test buy signal",
        )
        assert signal.signal_type == SignalType.BUY
        assert signal.reason == "Test buy signal"

    def test_signal_type_values(self) -> None:
        assert SignalType.BUY.value == "BUY"
        assert SignalType.SELL.value == "SELL"
        assert SignalType.HOLD.value == "HOLD"


class TestOMSOrder:
    """Tests for OMSOrder domain model."""

    def test_oms_order_construction(self) -> None:
        intent = OrderIntent(
            signal_id="SIG-100",
            strategy_id="MODEL_BLUE",
            action=None,
            legs=[],
            timestamp=datetime.now(UTC),
        )
        order = OMSOrder(
            internal_order_id="ORD-100",
            intent=intent,
            symbol="AAPL",
            side=RMSOrderSide.BUY,
            quantity=10,
            limit_price=Decimal("150.00"),
            order_type="LIMIT",
        )
        assert order.status == OrderStatus.PENDING
        assert order.filled_quantity == 0
        assert order.remaining_quantity == 10
        assert order.price == Decimal("150.00")
