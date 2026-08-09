"""Unit and integration tests for CandleStrategyProcessor."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest import mock

from app.broker.ibkr.tws_client import TWSClient
from app.core.config import Settings
from app.market_data.candle_builder import CandleBuilder
from app.market_data.candle_processor import MarketDataCandleProcessor
from app.market_data.ibkr_market_data import IBKRMarketDataAdapter
from app.market_data.simulator import MarketDataSimulator
from app.models.candle import Candle
from app.models.signal import Signal, SignalType
from app.services.order_manager import OrderManager
from app.strategy.base_strategy import BaseStrategy
from app.strategy.candle_strategy_processor import CandleStrategyProcessor
from app.strategy.five_candle_strategy import FiveCandleStrategy


def _candle(dt: datetime, open_p: float, close_p: float, volume: int = 100) -> Candle:
    """Helper to construct a Candle instance."""
    o = Decimal(str(open_p))
    c = Decimal(str(close_p))
    return Candle(
        timestamp=dt,
        open=o,
        high=max(o, c) + Decimal("1.0"),
        low=min(o, c) - Decimal("1.0"),
        close=c,
        volume=volume,
    )


class TestCandleStrategyProcessor:
    def test_dependency_injection(self) -> None:
        """Verify processor uses the injected strategy."""
        strategy = mock.Mock(spec=BaseStrategy)
        strategy.evaluate.return_value = Signal(
            signal_type=SignalType.HOLD,
            timestamp=datetime.now(UTC),
            reason="DI check",
        )
        processor = CandleStrategyProcessor(strategy=strategy)

        c = _candle(datetime(2025, 6, 15, 10, 0, tzinfo=UTC), 100.0, 105.0)
        processor.process_candle(c)

        strategy.evaluate.assert_called_once_with([c])

    def test_input_immutability(self) -> None:
        """Verify the processor does not mutate the Candle object and copies history list."""
        strategy = FiveCandleStrategy()
        processor = CandleStrategyProcessor(strategy=strategy)

        c = _candle(datetime(2025, 6, 15, 10, 0, tzinfo=UTC), 100.0, 105.0)
        processor.process_candle(c)

        # Ensure properties remain identical
        assert c.open == Decimal("100.0")
        assert c.close == Decimal("105.0")
        assert c.volume == 100

        # Ensure history getter returns a copy (mutating it should not modify processor state)
        history = processor.get_candles()
        assert len(history) == 1
        history.append(_candle(datetime.now(UTC), 99.0, 99.0))
        assert len(processor.get_candles()) == 1

    def test_chronological_history(self) -> None:
        """Verify candles are maintained in chronological order."""
        strategy = mock.Mock(spec=BaseStrategy)
        strategy.evaluate.return_value = Signal(
            signal_type=SignalType.HOLD,
            timestamp=datetime.now(UTC),
            reason="Mock reason",
        )
        processor = CandleStrategyProcessor(strategy=strategy)

        t0 = datetime(2025, 6, 15, 10, 0, tzinfo=UTC)
        c1 = _candle(t0, 100.0, 101.0)
        c2 = _candle(t0 + timedelta(minutes=5), 101.0, 102.0)

        processor.process_candle(c1)
        processor.process_candle(c2)

        # Check call arguments
        strategy.evaluate.assert_has_calls([mock.call([c1]), mock.call([c1, c2])])
        assert processor.get_candles() == [c1, c2]

    def test_insufficient_candles(self) -> None:
        """Test 1: Fewer than five completed candles results in HOLD."""
        strategy = FiveCandleStrategy()
        processor = CandleStrategyProcessor(strategy=strategy)

        t0 = datetime(2025, 6, 15, 10, 0, tzinfo=UTC)
        for i in range(4):
            c = _candle(t0 + timedelta(minutes=i * 5), 100.0, 101.0)  # Bullish
            sig = processor.process_candle(c)
            assert sig.signal_type == SignalType.HOLD

    def test_five_bullish_candles(self) -> None:
        """Test 2: Five consecutive bullish candles produces BUY."""
        strategy = FiveCandleStrategy()
        processor = CandleStrategyProcessor(strategy=strategy)

        t0 = datetime(2025, 6, 15, 10, 0, tzinfo=UTC)
        for i in range(4):
            c = _candle(t0 + timedelta(minutes=i * 5), 100.0, 101.0)
            processor.process_candle(c)

        # 5th bullish candle
        c5 = _candle(t0 + timedelta(minutes=20), 100.0, 101.0)
        sig = processor.process_candle(c5)
        assert sig.signal_type == SignalType.BUY

    def test_five_bearish_candles(self) -> None:
        """Test 3: Five consecutive bearish candles produces SELL."""
        strategy = FiveCandleStrategy()
        processor = CandleStrategyProcessor(strategy=strategy)

        t0 = datetime(2025, 6, 15, 10, 0, tzinfo=UTC)
        for i in range(4):
            c = _candle(t0 + timedelta(minutes=i * 5), 100.0, 99.0)
            processor.process_candle(c)

        # 5th bearish candle
        c5 = _candle(t0 + timedelta(minutes=20), 100.0, 99.0)
        sig = processor.process_candle(c5)
        assert sig.signal_type == SignalType.SELL

    def test_mixed_candles(self) -> None:
        """Test 4: Mixed bullish/bearish sequence produces HOLD."""
        strategy = FiveCandleStrategy()
        processor = CandleStrategyProcessor(strategy=strategy)

        t0 = datetime(2025, 6, 15, 10, 0, tzinfo=UTC)
        candles = [
            _candle(t0, 100, 101),  # Bull
            _candle(t0 + timedelta(minutes=5), 101, 100),  # Bear
            _candle(t0 + timedelta(minutes=10), 100, 101),  # Bull
            _candle(t0 + timedelta(minutes=15), 101, 100),  # Bear
            _candle(t0 + timedelta(minutes=20), 100, 101),  # Bull
        ]

        for c in candles[:-1]:
            processor.process_candle(c)

        sig = processor.process_candle(candles[-1])
        assert sig.signal_type == SignalType.HOLD

    def test_neutral_candle(self) -> None:
        """Test 5: Sequence containing a neutral candle produces HOLD."""
        strategy = FiveCandleStrategy()
        processor = CandleStrategyProcessor(strategy=strategy)

        t0 = datetime(2025, 6, 15, 10, 0, tzinfo=UTC)
        candles = [
            _candle(t0, 100, 101),  # Bull
            _candle(t0 + timedelta(minutes=5), 101, 102),  # Bull
            _candle(t0 + timedelta(minutes=10), 102, 102),  # Neutral
            _candle(t0 + timedelta(minutes=15), 102, 103),  # Bull
            _candle(t0 + timedelta(minutes=20), 103, 104),  # Bull
        ]

        for c in candles[:-1]:
            processor.process_candle(c)

        sig = processor.process_candle(candles[-1])
        assert sig.signal_type == SignalType.HOLD

    def test_latest_five_only(self) -> None:
        """Test 6: Old candles followed by five bullish/bearish candles.

        Verify only the latest five dictate the signal.
        """
        strategy = FiveCandleStrategy()
        processor = CandleStrategyProcessor(strategy=strategy)

        t0 = datetime(2025, 6, 15, 10, 0, tzinfo=UTC)
        # 1. Start with 2 bearish candles
        processor.process_candle(_candle(t0, 100, 99))
        processor.process_candle(_candle(t0 + timedelta(minutes=5), 99, 98))

        # 2. Feed 5 bullish candles
        for i in range(2, 6):
            processor.process_candle(_candle(t0 + timedelta(minutes=i * 5), 100, 101))

        sig = processor.process_candle(_candle(t0 + timedelta(minutes=30), 100, 101))
        assert sig.signal_type == SignalType.BUY

    def test_sequential_signal_generation(self) -> None:
        """Test 7: Feed five bullish -> BUY, then five bearish -> SELL."""
        strategy = FiveCandleStrategy()
        processor = CandleStrategyProcessor(strategy=strategy)

        t0 = datetime(2025, 6, 15, 10, 0, tzinfo=UTC)
        # 5 bullish
        for i in range(4):
            processor.process_candle(_candle(t0 + timedelta(minutes=i * 5), 100, 101))
        sig1 = processor.process_candle(_candle(t0 + timedelta(minutes=20), 100, 101))
        assert sig1.signal_type == SignalType.BUY

        # Followed by 5 bearish
        for i in range(5, 9):
            processor.process_candle(_candle(t0 + timedelta(minutes=i * 5), 100, 99))
        sig2 = processor.process_candle(_candle(t0 + timedelta(minutes=45), 100, 99))
        assert sig2.signal_type == SignalType.SELL

    def test_no_duplicate_evaluation(self) -> None:
        """Test 8: Verify strategy is evaluated exactly once per completed candle."""
        strategy = mock.Mock(spec=BaseStrategy)
        strategy.evaluate.return_value = Signal(
            signal_type=SignalType.HOLD,
            timestamp=datetime.now(UTC),
            reason="Mock reason",
        )
        processor = CandleStrategyProcessor(strategy=strategy)

        c = _candle(datetime(2025, 6, 15, 10, 0, tzinfo=UTC), 100.0, 101.0)
        processor.process_candle(c)

        strategy.evaluate.assert_called_once()

    def test_end_to_end_integration_simulator(self) -> None:
        """Test 12: Cover MarketDataEvent -> CandleBuilder -> Candle -> Strategy -> Signal.

        Uses MarketDataSimulator to feed deterministic ticks.
        """
        sim = MarketDataSimulator(
            symbol="AAPL",
            starting_price=Decimal("150.00"),
            patterns=["BULLISH", "BULLISH", "BULLISH", "BULLISH", "BULLISH"],
            start_time=datetime(2025, 6, 15, 10, 0, tzinfo=UTC),
            ticks_per_candle=5,
            tick_interval_seconds=60,
            volume=10,
        )

        builder = CandleBuilder(timeframe_minutes=5)
        processor = MarketDataCandleProcessor(candle_builder=builder)

        strategy = FiveCandleStrategy()
        strategy_processor = CandleStrategyProcessor(strategy=strategy)

        signals = []
        for e in sim.iter_events():
            candle = processor.process_event(e)
            if candle is not None:
                sig = strategy_processor.process_candle(candle)
                signals.append(sig)

        # The last signal must be BUY since we processed 5 consecutive completed bullish candles
        assert len(signals) == 5
        assert signals[-1].signal_type == SignalType.BUY
        assert all(s.signal_type == SignalType.HOLD for s in signals[:-1])

    def test_ibkr_path_integration(self) -> None:
        """Test 13: Verify that normalized IBKR adapter ticks flow through the strategy to generate BUY."""
        client = mock.Mock(spec=TWSClient)
        client.is_connected.return_value = True

        adapter = IBKRMarketDataAdapter(client, Settings())
        req_id = adapter.request_market_data()

        builder = CandleBuilder(timeframe_minutes=5)
        candle_processor = MarketDataCandleProcessor(candle_builder=builder)

        strategy = FiveCandleStrategy()
        strategy_processor = CandleStrategyProcessor(strategy=strategy)

        t0 = datetime(2025, 6, 15, 10, 0, tzinfo=UTC)

        # Generate ticks for 5 bullish candles + 1 boundary update to trigger 5th candle completion
        signals = []
        for i in range(5):
            # Candle i: starts at t0 + i*5m
            candle_start = t0 + timedelta(minutes=i * 5)
            # Low price at start
            with mock.patch("app.market_data.ibkr_market_data.datetime") as mock_dt:
                mock_dt.now.return_value = candle_start
                adapter.on_tick_price(req_id, 4, 100.0 + i)
                adapter.on_tick_size(req_id, 5, 20)

            # High price at end of candle (bullish)
            with mock.patch("app.market_data.ibkr_market_data.datetime") as mock_dt:
                mock_dt.now.return_value = candle_start + timedelta(minutes=4)
                adapter.on_tick_price(req_id, 4, 101.0 + i)
                adapter.on_tick_size(req_id, 5, 20)

        # Final boundary tick to trigger completion of the 5th candle
        with mock.patch("app.market_data.ibkr_market_data.datetime") as mock_dt:
            mock_dt.now.return_value = t0 + timedelta(minutes=25)
            adapter.on_tick_price(req_id, 4, 110.00)
            adapter.on_tick_size(req_id, 5, 10)

        # Feed all queue events sequentially
        while adapter.queue_size() > 0:
            event = adapter.get_event()
            assert event is not None
            candle = candle_processor.process_event(event)
            if candle is not None:
                sig = strategy_processor.process_candle(candle)
                signals.append(sig)

        assert len(signals) == 5
        # The 5th completed candle evaluation results in BUY
        assert signals[-1].signal_type == SignalType.BUY

    def test_private_state_isolation(self) -> None:
        """Verify TradingService does not require direct access to _candles."""
        from app.services.trading_service import TradingService

        builder = mock.Mock(spec=CandleBuilder)
        strategy = mock.Mock(spec=BaseStrategy)
        order_mgr = mock.Mock(spec=OrderManager)

        service = TradingService(builder, strategy, order_mgr)
        # Verify TradingService itself does not expose _candles
        assert not hasattr(service, "_candles")

    def test_public_history_access_defensive_copy(self) -> None:
        """Verify get_candles() returns a defensive copy of history."""
        strategy = FiveCandleStrategy()
        processor = CandleStrategyProcessor(strategy=strategy)

        c1 = _candle(datetime(2025, 6, 15, 10, 0, tzinfo=UTC), 100.0, 101.0)
        processor.process_candle(c1)

        history = processor.get_candles()
        # Modifying the returned list should not modify internal list
        history.append(_candle(datetime(2025, 6, 15, 10, 5, tzinfo=UTC), 101.0, 102.0))
        assert len(processor.get_candles()) == 1

    def test_public_history_access_chronological(self) -> None:
        """Verify get_candles() returns the chronological candle history."""
        strategy = FiveCandleStrategy()
        processor = CandleStrategyProcessor(strategy=strategy)

        t0 = datetime(2025, 6, 15, 10, 0, tzinfo=UTC)
        c1 = _candle(t0, 100.0, 101.0)
        c2 = _candle(t0 + timedelta(minutes=5), 101.0, 102.0)

        processor.process_candle(c1)
        processor.process_candle(c2)

        history = processor.get_candles()
        assert history == [c1, c2]
