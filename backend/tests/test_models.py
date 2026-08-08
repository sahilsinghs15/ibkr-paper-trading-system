"""Tests for domain models."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.broker import BrokerStatus, Margin
from app.models.candle import Candle
from app.models.order import Order, OrderSide, OrderStatus
from app.models.position import Position
from app.models.signal import Signal, SignalType

# ── Candle ──────────────────────────────────────────────────────────


class TestCandle:
    def test_bullish_candle(self) -> None:
        candle = Candle(
            timestamp=datetime(2025, 1, 1, 10, 0, tzinfo=UTC),
            open=Decimal("100.00"),
            high=Decimal("110.00"),
            low=Decimal("99.00"),
            close=Decimal("108.00"),
            volume=1000,
        )
        assert candle.is_bullish is True
        assert candle.is_bearish is False

    def test_bearish_candle(self) -> None:
        candle = Candle(
            timestamp=datetime(2025, 1, 1, 10, 0, tzinfo=UTC),
            open=Decimal("108.00"),
            high=Decimal("110.00"),
            low=Decimal("99.00"),
            close=Decimal("100.00"),
            volume=1000,
        )
        assert candle.is_bullish is False
        assert candle.is_bearish is True

    def test_neutral_candle(self) -> None:
        candle = Candle(
            timestamp=datetime(2025, 1, 1, 10, 0, tzinfo=UTC),
            open=Decimal("100.00"),
            high=Decimal("110.00"),
            low=Decimal("99.00"),
            close=Decimal("100.00"),
            volume=1000,
        )
        assert candle.is_bullish is False
        assert candle.is_bearish is False

    def test_candle_is_immutable(self) -> None:
        candle = Candle(
            timestamp=datetime(2025, 1, 1, 10, 0, tzinfo=UTC),
            open=Decimal(100),
            high=Decimal(110),
            low=Decimal(90),
            close=Decimal(105),
            volume=500,
        )
        with pytest.raises(AttributeError):
            candle.close = Decimal(999)  # type: ignore[misc]


# ── Position ────────────────────────────────────────────────────────


class TestPosition:
    def test_is_flat_when_zero(self) -> None:
        pos = Position(
            symbol="RELIANCE",
            quantity=0,
            average_price=Decimal("2500.00"),
        )
        assert pos.is_flat is True

    def test_is_not_flat_when_holding(self) -> None:
        pos = Position(
            symbol="RELIANCE",
            quantity=10,
            average_price=Decimal("2500.00"),
        )
        assert pos.is_flat is False

    def test_default_pnl_values(self) -> None:
        pos = Position(
            symbol="RELIANCE",
            quantity=5,
            average_price=Decimal("2500.00"),
        )
        assert pos.unrealized_pnl == Decimal(0)
        assert pos.realized_pnl == Decimal(0)


# ── Signal ──────────────────────────────────────────────────────────


class TestSignal:
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


# ── Order ───────────────────────────────────────────────────────────


class TestOrder:
    def test_order_construction(self) -> None:
        order = Order(
            order_id="ORD-001",
            symbol="RELIANCE",
            side=OrderSide.BUY,
            quantity=10,
            order_type="LIMIT",
            price=Decimal("2500.00"),
        )
        assert order.status == OrderStatus.PENDING
        assert order.filled_quantity == 0
        assert order.average_fill_price is None

    def test_order_side_values(self) -> None:
        assert OrderSide.BUY.value == "BUY"
        assert OrderSide.SELL.value == "SELL"

    def test_order_status_values(self) -> None:
        assert OrderStatus.PENDING.value == "PENDING"
        assert OrderStatus.SUBMITTED.value == "SUBMITTED"
        assert OrderStatus.PARTIALLY_FILLED.value == "PARTIALLY_FILLED"
        assert OrderStatus.FILLED.value == "FILLED"
        assert OrderStatus.CANCELLED.value == "CANCELLED"
        assert OrderStatus.REJECTED.value == "REJECTED"

    def test_market_order_no_price(self) -> None:
        order = Order(
            order_id="ORD-002",
            symbol="RELIANCE",
            side=OrderSide.BUY,
            quantity=5,
            order_type="MARKET",
        )
        assert order.price is None


# ── BrokerStatus / Margin ──────────────────────────────────────────


class TestBrokerModels:
    def test_broker_status_values(self) -> None:
        assert BrokerStatus.DISCONNECTED.value == "DISCONNECTED"
        assert BrokerStatus.CONNECTING.value == "CONNECTING"
        assert BrokerStatus.CONNECTED.value == "CONNECTED"
        assert BrokerStatus.RECONNECTING.value == "RECONNECTING"
        assert BrokerStatus.ERROR.value == "ERROR"

    def test_margin_construction(self) -> None:
        margin = Margin(
            equity=Decimal(100000),
            available_funds=Decimal(80000),
            buying_power=Decimal(160000),
        )
        assert margin.equity == Decimal(100000)
        assert margin.available_funds == Decimal(80000)
        assert margin.buying_power == Decimal(160000)
