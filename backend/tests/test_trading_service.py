"""Tests for TradingService — Phase 2.5."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, Mock, call

import pytest

from app.broker.base_broker import BaseBroker
from app.market_data.candle_builder import CandleBuilder
from app.models.candle import Candle
from app.models.market_data import MarketDataEvent
from app.models.order import Order, OrderSide, OrderStatus
from app.models.signal import Signal, SignalType
from app.services.order_manager import OrderManager
from app.services.trading_service import TradingService
from app.strategy.base_strategy import BaseStrategy
from app.strategy.five_candle_strategy import FiveCandleStrategy


def _event(dt: datetime, price: str, volume: int) -> MarketDataEvent:
    return MarketDataEvent(timestamp=dt, price=Decimal(price), volume=volume)


def _candle(dt: datetime, open_p: str, close_p: str, volume: int) -> Candle:
    dec_open = Decimal(open_p)
    dec_close = Decimal(close_p)
    return Candle(
        timestamp=dt,
        open=dec_open,
        high=max(dec_open, dec_close),
        low=min(dec_open, dec_close),
        close=dec_close,
        volume=volume,
    )


@pytest.mark.asyncio
class TestTradingService:
    async def test_incomplete_candle_returns_none(self) -> None:
        """When CandleBuilder returns None:

        - strategy is NOT called
        - OrderManager is NOT called
        - process returns None
        """
        mock_builder = Mock(spec=CandleBuilder)
        mock_builder.add_event.return_value = None
        mock_strategy = Mock(spec=BaseStrategy)
        mock_order_mgr = AsyncMock(spec=OrderManager)

        service = TradingService(mock_builder, mock_strategy, mock_order_mgr)
        event = _event(datetime(2025, 1, 1, 10, 0, tzinfo=UTC), "100", 10)

        result = await service.process_market_data(event)

        assert result is None
        mock_builder.add_event.assert_called_once_with(event)
        mock_strategy.evaluate.assert_not_called()
        mock_order_mgr.process_signal.assert_not_called()

    async def test_completed_candle_triggers_strategy_and_order_manager(self) -> None:
        """When CandleBuilder returns a completed candle:

        - candle is added to history
        - strategy is called
        - BUY signal is passed to OrderManager
        - successful OrderManager result is returned
        """
        mock_builder = Mock(spec=CandleBuilder)
        candle = _candle(datetime(2025, 1, 1, 10, 0, tzinfo=UTC), "100", "105", 100)
        mock_builder.add_event.return_value = candle

        mock_strategy = Mock(spec=BaseStrategy)
        signal = Signal(
            signal_type=SignalType.BUY,
            timestamp=datetime(2025, 1, 1, 10, 5, tzinfo=UTC),
            reason="Bullish",
        )
        mock_strategy.evaluate.return_value = signal

        mock_order_mgr = AsyncMock(spec=OrderManager)
        order = Order(
            order_id="1",
            symbol="RELIANCE",
            side=OrderSide.BUY,
            quantity=10,
            order_type="MARKET",
            status=OrderStatus.SUBMITTED,
            timestamp=datetime(2025, 1, 1, 10, 5, tzinfo=UTC),
        )
        mock_order_mgr.process_signal.return_value = order

        service = TradingService(mock_builder, mock_strategy, mock_order_mgr)
        event = _event(datetime(2025, 1, 1, 10, 5, tzinfo=UTC), "105", 10)

        result = await service.process_market_data(event)

        assert result == (signal, order)
        mock_builder.add_event.assert_called_once_with(event)
        mock_strategy.evaluate.assert_called_once_with([candle])
        mock_order_mgr.process_signal.assert_called_once_with(signal)

    async def test_sell_signal_is_passed_to_order_manager(self) -> None:
        """SELL signal is passed to OrderManager."""
        mock_builder = Mock(spec=CandleBuilder)
        candle = _candle(datetime(2025, 1, 1, 10, 0, tzinfo=UTC), "100", "95", 100)
        mock_builder.add_event.return_value = candle

        mock_strategy = Mock(spec=BaseStrategy)
        signal = Signal(
            signal_type=SignalType.SELL,
            timestamp=datetime(2025, 1, 1, 10, 5, tzinfo=UTC),
            reason="Bearish",
        )
        mock_strategy.evaluate.return_value = signal

        mock_order_mgr = AsyncMock(spec=OrderManager)
        order = Order(
            order_id="2",
            symbol="RELIANCE",
            side=OrderSide.SELL,
            quantity=10,
            order_type="MARKET",
            status=OrderStatus.SUBMITTED,
            timestamp=datetime(2025, 1, 1, 10, 5, tzinfo=UTC),
        )
        mock_order_mgr.process_signal.return_value = order

        service = TradingService(mock_builder, mock_strategy, mock_order_mgr)
        event = _event(datetime(2025, 1, 1, 10, 5, tzinfo=UTC), "95", 10)

        result = await service.process_market_data(event)

        assert result == (signal, order)
        mock_order_mgr.process_signal.assert_called_once_with(signal)

    async def test_hold_signal_is_passed_to_order_manager(self) -> None:
        """HOLD signal is also passed to OrderManager."""
        mock_builder = Mock(spec=CandleBuilder)
        candle = _candle(datetime(2025, 1, 1, 10, 0, tzinfo=UTC), "100", "100", 100)
        mock_builder.add_event.return_value = candle

        mock_strategy = Mock(spec=BaseStrategy)
        signal = Signal(
            signal_type=SignalType.HOLD,
            timestamp=datetime(2025, 1, 1, 10, 5, tzinfo=UTC),
            reason="Neutral",
        )
        mock_strategy.evaluate.return_value = signal

        mock_order_mgr = AsyncMock(spec=OrderManager)
        mock_order_mgr.process_signal.return_value = None

        service = TradingService(mock_builder, mock_strategy, mock_order_mgr)
        event = _event(datetime(2025, 1, 1, 10, 5, tzinfo=UTC), "100", 10)

        result = await service.process_market_data(event)
        assert result == (signal, None)
        mock_order_mgr.process_signal.assert_called_once_with(signal)

    async def test_chronological_order_of_history(self) -> None:
        """Strategy receives candles in chronological order.

        Multiple completed candles are accumulated in chronological
        order.
        """
        mock_builder = Mock(spec=CandleBuilder)
        candle1 = _candle(datetime(2025, 1, 1, 10, 0, tzinfo=UTC), "100", "101", 100)
        candle2 = _candle(datetime(2025, 1, 1, 10, 5, tzinfo=UTC), "101", "102", 100)

        mock_builder.add_event.side_effect = [candle1, candle2]

        mock_strategy = Mock(spec=BaseStrategy)
        mock_strategy.evaluate.return_value = Signal(
            signal_type=SignalType.HOLD,
            timestamp=datetime(2025, 1, 1, 10, 10, tzinfo=UTC),
            reason="testing",
        )

        mock_order_mgr = AsyncMock(spec=OrderManager)
        mock_order_mgr.process_signal.return_value = None

        service = TradingService(mock_builder, mock_strategy, mock_order_mgr)

        event1 = _event(datetime(2025, 1, 1, 10, 5, tzinfo=UTC), "101", 10)
        event2 = _event(datetime(2025, 1, 1, 10, 10, tzinfo=UTC), "102", 10)

        await service.process_market_data(event1)
        await service.process_market_data(event2)

        mock_strategy.evaluate.assert_has_calls(
            [call([candle1]), call([candle1, candle2])]
        )

    async def test_exceptions_propagate(self) -> None:
        """Exceptions from dependencies propagate correctly."""
        # CandleBuilder exception propagates.
        mock_builder = Mock(spec=CandleBuilder)
        mock_builder.add_event.side_effect = ValueError("builder error")
        service = TradingService(
            mock_builder, Mock(spec=BaseStrategy), AsyncMock(spec=OrderManager)
        )
        event = _event(datetime(2025, 1, 1, 10, 0, tzinfo=UTC), "100", 10)

        with pytest.raises(ValueError, match="builder error"):
            await service.process_market_data(event)

        # Strategy exception propagates.
        mock_builder = Mock(spec=CandleBuilder)
        mock_builder.add_event.return_value = _candle(
            datetime(2025, 1, 1, 10, 0, tzinfo=UTC), "100", "101", 100
        )
        mock_strategy = Mock(spec=BaseStrategy)
        mock_strategy.evaluate.side_effect = RuntimeError("strategy error")
        service = TradingService(
            mock_builder, mock_strategy, AsyncMock(spec=OrderManager)
        )

        with pytest.raises(RuntimeError, match="strategy error"):
            await service.process_market_data(event)

        # OrderManager exception propagates.
        mock_strategy = Mock(spec=BaseStrategy)
        mock_strategy.evaluate.return_value = Signal(
            signal_type=SignalType.BUY,
            timestamp=datetime(2025, 1, 1, 10, 5, tzinfo=UTC),
            reason="Buy",
        )
        mock_order_mgr = AsyncMock(spec=OrderManager)
        mock_order_mgr.process_signal.side_effect = KeyError("order manager error")
        service = TradingService(mock_builder, mock_strategy, mock_order_mgr)

        with pytest.raises(KeyError, match="order manager error"):
            await service.process_market_data(event)

    async def test_dependency_injection_works(self) -> None:
        """TradingService works entirely with injected dependencies and does not

        instantiate concrete classes internally.
        """
        builder = Mock(spec=CandleBuilder)
        strategy = Mock(spec=BaseStrategy)
        order_mgr = AsyncMock(spec=OrderManager)

        service = TradingService(
            candle_builder=builder, strategy=strategy, order_manager=order_mgr
        )
        assert service._candle_builder is builder
        assert service._strategy is strategy
        assert service._order_manager is order_mgr

    async def test_e2e_orchestration_flow(self) -> None:
        """Test the complete orchestration flow end-to-end using real

        CandleBuilder, FiveCandleStrategy, and a mocked broker via OrderManager.
        """
        stub_broker = AsyncMock(spec=BaseBroker)
        stub_broker.place_order.return_value = Order(
            order_id="MOCK-ORD",
            symbol="RELIANCE",
            side=OrderSide.BUY,
            quantity=1,
            order_type="MARKET",
            status=OrderStatus.SUBMITTED,
            timestamp=datetime(2025, 6, 15, 10, 25, tzinfo=UTC),
        )

        # Instantiating dependencies from outside (DI)
        builder = CandleBuilder(timeframe_minutes=5)
        strategy = FiveCandleStrategy()
        order_mgr = OrderManager(
            stub_broker, symbol="RELIANCE", quantity=1, order_type="MARKET"
        )

        service = TradingService(builder, strategy, order_mgr)

        # We will feed events to form 5 completed bullish candles.
        # Candle 1: starts 10:00, updates at 10:04, completes at 10:05 (open=100, close=101)
        res = await service.process_market_data(
            _event(datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC), "100", 10)
        )
        assert res is None
        res = await service.process_market_data(
            _event(datetime(2025, 6, 15, 10, 4, 0, tzinfo=UTC), "101", 10)
        )
        assert res is None
        res = await service.process_market_data(
            _event(datetime(2025, 6, 15, 10, 5, 0, tzinfo=UTC), "101", 10)
        )
        assert res is not None
        signal, order = res
        assert signal.signal_type == SignalType.HOLD
        assert order is None

        # Candle 2: starts 10:05, updates at 10:09, completes at 10:10 (open=101, close=102)
        res = await service.process_market_data(
            _event(datetime(2025, 6, 15, 10, 9, 0, tzinfo=UTC), "102", 10)
        )
        assert res is None
        res = await service.process_market_data(
            _event(datetime(2025, 6, 15, 10, 10, 0, tzinfo=UTC), "102", 10)
        )
        assert res is not None
        signal, order = res
        assert signal.signal_type == SignalType.HOLD
        assert order is None

        # Candle 3: starts 10:10, updates at 10:14, completes at 10:15 (open=102, close=103)
        res = await service.process_market_data(
            _event(datetime(2025, 6, 15, 10, 14, 0, tzinfo=UTC), "103", 10)
        )
        assert res is None
        res = await service.process_market_data(
            _event(datetime(2025, 6, 15, 10, 15, 0, tzinfo=UTC), "103", 10)
        )
        assert res is not None
        signal, order = res
        assert signal.signal_type == SignalType.HOLD
        assert order is None

        # Candle 4: starts 10:15, updates at 10:19, completes at 10:20 (open=103, close=104)
        res = await service.process_market_data(
            _event(datetime(2025, 6, 15, 10, 19, 0, tzinfo=UTC), "104", 10)
        )
        assert res is None
        res = await service.process_market_data(
            _event(datetime(2025, 6, 15, 10, 20, 0, tzinfo=UTC), "104", 10)
        )
        assert res is not None
        signal, order = res
        assert signal.signal_type == SignalType.HOLD
        assert order is None

        # Candle 5: starts 10:20, updates at 10:24, completes at 10:25 (open=104, close=105)
        res = await service.process_market_data(
            _event(datetime(2025, 6, 15, 10, 24, 0, tzinfo=UTC), "105", 10)
        )
        assert res is None
        res = await service.process_market_data(
            _event(datetime(2025, 6, 15, 10, 25, 0, tzinfo=UTC), "105", 10)
        )
        assert res is not None
        signal, order = res
        assert signal.signal_type == SignalType.BUY
        assert order is not None
        assert order.side == OrderSide.BUY
        assert order.symbol == "RELIANCE"

        stub_broker.place_order.assert_called_once_with(
            symbol="RELIANCE",
            side=OrderSide.BUY,
            quantity=1,
            order_type="MARKET",
        )
