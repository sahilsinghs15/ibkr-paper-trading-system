"""Tests for OrderManager — Phase 2.4.

Uses an async stub of BaseBroker to test OrderManager behaviour
independently of any concrete broker implementation.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.broker.base_broker import BaseBroker
from app.models.order import Order, OrderSide, OrderStatus
from app.models.signal import Signal, SignalType
from app.services.order_manager import OrderManager

# ── test doubles ─────────────────────────────────────────────────────

_SYMBOL = "RELIANCE"
_QUANTITY = 1
_ORDER_TYPE = "MARKET"


def _make_broker_stub() -> AsyncMock:
    """Create a BaseBroker stub with an async ``place_order`` mock."""
    stub = AsyncMock(spec=BaseBroker)
    stub.place_order.return_value = Order(
        order_id="TEST-001",
        symbol=_SYMBOL,
        side=OrderSide.BUY,
        quantity=_QUANTITY,
        order_type=_ORDER_TYPE,
        status=OrderStatus.SUBMITTED,
    )
    return stub


def _signal(signal_type: SignalType) -> Signal:
    """Create a deterministic Signal."""
    return Signal(
        signal_type=signal_type,
        timestamp=datetime(2025, 6, 15, 10, 5, 0, tzinfo=UTC),
        reason="test signal",
    )


def _manager(broker: BaseBroker | None = None) -> OrderManager:
    """Create an OrderManager with defaults."""
    return OrderManager(
        broker=broker or _make_broker_stub(),
        symbol=_SYMBOL,
        quantity=_QUANTITY,
        order_type=_ORDER_TYPE,
    )


# ── BUY ──────────────────────────────────────────────────────────────


class TestBuy:
    """Tests 1-6: BUY signal behaviour."""

    @pytest.mark.asyncio
    async def test_buy_calls_place_order_once(self) -> None:
        broker = _make_broker_stub()
        mgr = _manager(broker)
        await mgr.process_signal(_signal(SignalType.BUY))
        broker.place_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_buy_uses_buy_side(self) -> None:
        broker = _make_broker_stub()
        mgr = _manager(broker)
        await mgr.process_signal(_signal(SignalType.BUY))
        _, kwargs = broker.place_order.call_args
        assert kwargs["side"] == OrderSide.BUY

    @pytest.mark.asyncio
    async def test_buy_uses_configured_symbol(self) -> None:
        broker = _make_broker_stub()
        mgr = _manager(broker)
        await mgr.process_signal(_signal(SignalType.BUY))
        _, kwargs = broker.place_order.call_args
        assert kwargs["symbol"] == _SYMBOL

    @pytest.mark.asyncio
    async def test_buy_uses_configured_quantity(self) -> None:
        broker = _make_broker_stub()
        mgr = _manager(broker)
        await mgr.process_signal(_signal(SignalType.BUY))
        _, kwargs = broker.place_order.call_args
        assert kwargs["quantity"] == _QUANTITY

    @pytest.mark.asyncio
    async def test_buy_uses_configured_order_type(self) -> None:
        broker = _make_broker_stub()
        mgr = _manager(broker)
        await mgr.process_signal(_signal(SignalType.BUY))
        _, kwargs = broker.place_order.call_args
        assert kwargs["order_type"] == _ORDER_TYPE

    @pytest.mark.asyncio
    async def test_buy_returns_broker_order(self) -> None:
        broker = _make_broker_stub()
        expected_order = Order(
            order_id="BUY-123",
            symbol=_SYMBOL,
            side=OrderSide.BUY,
            quantity=_QUANTITY,
            order_type=_ORDER_TYPE,
            status=OrderStatus.SUBMITTED,
        )
        broker.place_order.return_value = expected_order
        mgr = _manager(broker)
        result = await mgr.process_signal(_signal(SignalType.BUY))
        assert result is expected_order


# ── SELL ─────────────────────────────────────────────────────────────


class TestSell:
    """Tests 7-12: SELL signal behaviour."""

    @pytest.mark.asyncio
    async def test_sell_calls_place_order_once(self) -> None:
        broker = _make_broker_stub()
        mgr = _manager(broker)
        await mgr.process_signal(_signal(SignalType.SELL))
        broker.place_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_sell_uses_sell_side(self) -> None:
        broker = _make_broker_stub()
        mgr = _manager(broker)
        await mgr.process_signal(_signal(SignalType.SELL))
        _, kwargs = broker.place_order.call_args
        assert kwargs["side"] == OrderSide.SELL

    @pytest.mark.asyncio
    async def test_sell_uses_configured_symbol(self) -> None:
        broker = _make_broker_stub()
        mgr = _manager(broker)
        await mgr.process_signal(_signal(SignalType.SELL))
        _, kwargs = broker.place_order.call_args
        assert kwargs["symbol"] == _SYMBOL

    @pytest.mark.asyncio
    async def test_sell_uses_configured_quantity(self) -> None:
        broker = _make_broker_stub()
        mgr = _manager(broker)
        await mgr.process_signal(_signal(SignalType.SELL))
        _, kwargs = broker.place_order.call_args
        assert kwargs["quantity"] == _QUANTITY

    @pytest.mark.asyncio
    async def test_sell_uses_configured_order_type(self) -> None:
        broker = _make_broker_stub()
        mgr = _manager(broker)
        await mgr.process_signal(_signal(SignalType.SELL))
        _, kwargs = broker.place_order.call_args
        assert kwargs["order_type"] == _ORDER_TYPE

    @pytest.mark.asyncio
    async def test_sell_returns_broker_order(self) -> None:
        broker = _make_broker_stub()
        expected_order = Order(
            order_id="SELL-456",
            symbol=_SYMBOL,
            side=OrderSide.SELL,
            quantity=_QUANTITY,
            order_type=_ORDER_TYPE,
            status=OrderStatus.SUBMITTED,
        )
        broker.place_order.return_value = expected_order
        mgr = _manager(broker)
        result = await mgr.process_signal(_signal(SignalType.SELL))
        assert result is expected_order


# ── HOLD ─────────────────────────────────────────────────────────────


class TestHold:
    """Tests 13-14: HOLD signal behaviour."""

    @pytest.mark.asyncio
    async def test_hold_does_not_call_place_order(self) -> None:
        broker = _make_broker_stub()
        mgr = _manager(broker)
        await mgr.process_signal(_signal(SignalType.HOLD))
        broker.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_hold_returns_none(self) -> None:
        mgr = _manager()
        result = await mgr.process_signal(_signal(SignalType.HOLD))
        assert result is None


# ── Error handling ───────────────────────────────────────────────────


class TestErrorHandling:
    """Tests 15-16: broker errors propagate."""

    @pytest.mark.asyncio
    async def test_broker_exception_is_not_swallowed(self) -> None:
        broker = _make_broker_stub()
        broker.place_order.side_effect = RuntimeError("connection lost")
        mgr = _manager(broker)
        with pytest.raises(RuntimeError, match="connection lost"):
            await mgr.process_signal(_signal(SignalType.BUY))

    @pytest.mark.asyncio
    async def test_broker_exception_propagates_for_sell(self) -> None:
        broker = _make_broker_stub()
        broker.place_order.side_effect = ValueError("invalid order")
        mgr = _manager(broker)
        with pytest.raises(ValueError, match="invalid order"):
            await mgr.process_signal(_signal(SignalType.SELL))


# ── Dependency injection ────────────────────────────────────────────


class TestDependencyInjection:
    """Tests 17-18: works with BaseBroker abstraction."""

    @pytest.mark.asyncio
    async def test_works_with_base_broker_stub(self) -> None:
        """OrderManager works with any BaseBroker test double."""
        broker = _make_broker_stub()
        mgr = OrderManager(
            broker=broker,
            symbol="INFY",
            quantity=10,
            order_type="LIMIT",
        )
        result = await mgr.process_signal(_signal(SignalType.BUY))
        assert result is not None
        _, kwargs = broker.place_order.call_args
        assert kwargs["symbol"] == "INFY"
        assert kwargs["quantity"] == 10
        assert kwargs["order_type"] == "LIMIT"

    @pytest.mark.asyncio
    async def test_does_not_require_mock_broker(self) -> None:
        """OrderManager accepts any BaseBroker spec, not just MockBroker."""
        stub = AsyncMock(spec=BaseBroker)
        stub.place_order.return_value = Order(
            order_id="STUB-1",
            symbol=_SYMBOL,
            side=OrderSide.BUY,
            quantity=_QUANTITY,
            order_type=_ORDER_TYPE,
            status=OrderStatus.SUBMITTED,
        )
        mgr = OrderManager(
            broker=stub,
            symbol=_SYMBOL,
            quantity=_QUANTITY,
        )
        result = await mgr.process_signal(_signal(SignalType.BUY))
        assert result is not None
        assert result.order_id == "STUB-1"


# ── Input / state ───────────────────────────────────────────────────


class TestInputAndState:
    """Tests 19-20: signal immutability and no unnecessary state."""

    @pytest.mark.asyncio
    async def test_signal_is_not_mutated(self) -> None:
        signal = _signal(SignalType.BUY)
        original_type = signal.signal_type
        original_ts = signal.timestamp
        original_reason = signal.reason

        mgr = _manager()
        await mgr.process_signal(signal)

        assert signal.signal_type == original_type
        assert signal.timestamp == original_ts
        assert signal.reason == original_reason

    @pytest.mark.asyncio
    async def test_no_order_state_retained(self) -> None:
        """Processing multiple signals does not accumulate state."""
        broker = _make_broker_stub()
        mgr = _manager(broker)

        await mgr.process_signal(_signal(SignalType.BUY))
        await mgr.process_signal(_signal(SignalType.SELL))
        await mgr.process_signal(_signal(SignalType.HOLD))

        # BUY + SELL = 2 calls; HOLD = 0
        assert broker.place_order.call_count == 2
