"""Unit and integration tests for MarketDataCandleProcessor."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest import mock

import pytest

from app.broker.ibkr.tws_client import TWSClient
from app.core.config import Settings
from app.market_data.candle_builder import CandleBuilder
from app.market_data.candle_processor import MarketDataCandleProcessor
from app.market_data.ibkr_market_data import IBKRMarketDataAdapter
from app.market_data.simulator import MarketDataSimulator
from app.models.candle import Candle
from app.models.market_data import MarketDataEvent


class TestCandleProcessor:
    def test_dependency_injection(self) -> None:
        """Verify the processor uses the injected CandleBuilder."""
        builder = mock.Mock(spec=CandleBuilder)
        builder.add_event.return_value = None
        processor = MarketDataCandleProcessor(candle_builder=builder)

        event = MarketDataEvent(
            timestamp=datetime(2025, 6, 15, 10, 0, tzinfo=UTC),
            price=Decimal("100.0"),
            volume=50,
        )
        processor.process_event(event)
        builder.add_event.assert_called_once_with(event)

    def test_input_integrity(self) -> None:
        """Verify the processor does not mutate input MarketDataEvent objects."""
        builder = CandleBuilder(timeframe_minutes=5)
        processor = MarketDataCandleProcessor(candle_builder=builder)

        event = MarketDataEvent(
            timestamp=datetime(2025, 6, 15, 10, 0, tzinfo=UTC),
            price=Decimal("100.0"),
            volume=50,
        )

        processor.process_event(event)

        assert event.timestamp == datetime(2025, 6, 15, 10, 0, tzinfo=UTC)
        assert event.price == Decimal("100.0")
        assert event.volume == 50

    def test_basic_processing_flow(self) -> None:
        """Verify events process correctly and emit completed candles at boundaries."""
        builder = CandleBuilder(timeframe_minutes=5)
        processor = MarketDataCandleProcessor(candle_builder=builder)

        t0 = datetime(2025, 6, 15, 10, 0, tzinfo=UTC)

        # 1. Single event inside interval
        res = processor.process_event(MarketDataEvent(t0, Decimal("100.00"), 10))
        assert res is None

        # 2. Multiple events inside same interval
        res = processor.process_event(
            MarketDataEvent(t0 + timedelta(seconds=30), Decimal("101.00"), 20)
        )
        assert res is None

        # 3. Event in the next interval triggers completion of the previous one
        res = processor.process_event(
            MarketDataEvent(t0 + timedelta(minutes=5), Decimal("102.00"), 15)
        )
        assert res is not None
        assert isinstance(res, Candle)
        assert res.timestamp == t0
        assert res.open == Decimal("100.00")
        assert res.high == Decimal("101.00")
        assert res.low == Decimal("100.00")
        assert res.close == Decimal("101.00")
        assert res.volume == 30
        assert res.is_bullish is True
        assert res.is_bearish is False

    def test_candle_direction_neutral(self) -> None:
        """Verify neutral candle direction behavior."""
        builder = CandleBuilder(timeframe_minutes=5)
        processor = MarketDataCandleProcessor(candle_builder=builder)
        t0 = datetime(2025, 6, 15, 10, 0, tzinfo=UTC)

        processor.process_event(MarketDataEvent(t0, Decimal("100.00"), 10))

        # Boundary shift with flat price changes
        res = processor.process_event(
            MarketDataEvent(t0 + timedelta(minutes=5), Decimal("100.00"), 5)
        )
        assert res is not None
        assert res.open == res.close
        assert res.is_bullish is False
        assert res.is_bearish is False

    def test_multiple_candles_sequential(self) -> None:
        """Verify sequential emission over multiple intervals."""
        builder = CandleBuilder(timeframe_minutes=5)
        processor = MarketDataCandleProcessor(candle_builder=builder)
        t0 = datetime(2025, 6, 15, 10, 0, tzinfo=UTC)

        # Interval 1
        processor.process_event(MarketDataEvent(t0, Decimal("100.00"), 10))

        # Interval 2 - triggers Candle 1
        res1 = processor.process_event(
            MarketDataEvent(t0 + timedelta(minutes=5), Decimal("99.00"), 20)
        )
        assert res1 is not None
        assert res1.timestamp == t0

        # Interval 3 - triggers Candle 2
        res2 = processor.process_event(
            MarketDataEvent(t0 + timedelta(minutes=10), Decimal("98.00"), 30)
        )
        assert res2 is not None
        assert res2.timestamp == t0 + timedelta(minutes=5)

    def test_gaps_do_not_produce_synthetic_candles(self) -> None:
        """Verify interval gaps do not output synthetic/empty candles."""
        builder = CandleBuilder(timeframe_minutes=5)
        processor = MarketDataCandleProcessor(candle_builder=builder)
        t0 = datetime(2025, 6, 15, 10, 0, tzinfo=UTC)

        # Interval 1
        processor.process_event(MarketDataEvent(t0, Decimal("100.00"), 10))

        # Skip 2 intervals (10 minutes) and send event in Interval 4
        res = processor.process_event(
            MarketDataEvent(t0 + timedelta(minutes=15), Decimal("102.00"), 20)
        )

        # Should return Candle 1 (at t0)
        assert res is not None
        assert res.timestamp == t0

        # The next event (in Interval 5) will trigger Candle 4 (at t0 + 15m)
        res2 = processor.process_event(
            MarketDataEvent(t0 + timedelta(minutes=20), Decimal("103.00"), 10)
        )
        assert res2 is not None
        assert res2.timestamp == t0 + timedelta(minutes=15)

    def test_out_of_order_validation_propagates(self) -> None:
        """Verify out-of-order events raise ValueError and propagate cleanly."""
        builder = CandleBuilder(timeframe_minutes=5)
        processor = MarketDataCandleProcessor(candle_builder=builder)
        t0 = datetime(2025, 6, 15, 10, 0, tzinfo=UTC)

        processor.process_event(MarketDataEvent(t0, Decimal("100.0"), 10))

        # Event in the past
        with pytest.raises(ValueError, match="Out-of-order event"):
            processor.process_event(
                MarketDataEvent(t0 - timedelta(seconds=1), Decimal("100.0"), 10)
            )

    def test_integration_adapter_to_processor(self) -> None:
        """Integration test verifying end-to-end flow from adapter to processor."""
        client = mock.Mock(spec=TWSClient)
        client.is_connected.return_value = True

        settings = Settings()
        adapter = IBKRMarketDataAdapter(client, settings)
        req_id = adapter.request_market_data()

        builder = CandleBuilder(timeframe_minutes=5)
        processor = MarketDataCandleProcessor(candle_builder=builder)

        t0 = datetime(2025, 6, 15, 10, 0, tzinfo=UTC)

        with mock.patch("app.market_data.ibkr_market_data.datetime") as mock_dt:
            # 1. First trade update (Sequence A)
            mock_dt.now.return_value = t0
            adapter.on_tick_price(req_id, 4, 100.00)
            adapter.on_tick_size(req_id, 5, 50)

            # 2. Second trade update (Sequence B)
            mock_dt.now.return_value = t0 + timedelta(minutes=2)
            adapter.on_tick_size(req_id, 5, 30)
            adapter.on_tick_price(req_id, 4, 102.00)

            # 3. Boundary trade update (Sequence A) - triggers completed candle
            mock_dt.now.return_value = t0 + timedelta(minutes=5)
            adapter.on_tick_price(req_id, 4, 101.00)
            adapter.on_tick_size(req_id, 5, 10)

        # Drain queue and process
        candles = []
        while adapter.queue_size() > 0:
            event = adapter.get_event()
            assert event is not None
            c = processor.process_event(event)
            if c is not None:
                candles.append(c)

        assert len(candles) == 1
        candle = candles[0]
        assert candle.timestamp == t0
        assert candle.open == Decimal("100.00")
        assert candle.high == Decimal("102.00")
        assert candle.low == Decimal("100.00")
        assert candle.close == Decimal("102.00")
        assert candle.volume == 80

    def test_comparison_simulator_vs_adapter(self) -> None:
        """Comparison test verifying equivalent candle output from simulator and adapter."""
        # 1. Run simulator events directly through CandleBuilder
        sim = MarketDataSimulator(
            symbol="AAPL",
            starting_price=Decimal("150.00"),
            patterns=["BULLISH", "BEARISH"],
            start_time=datetime(2025, 6, 15, 10, 0, tzinfo=UTC),
            ticks_per_candle=5,
            tick_interval_seconds=60,
            volume=10,
        )

        sim_builder = CandleBuilder(timeframe_minutes=5)
        sim_processor = MarketDataCandleProcessor(candle_builder=sim_builder)

        sim_candles = []
        ticks = list(sim.iter_events())

        for e in ticks:
            c = sim_processor.process_event(e)
            if c is not None:
                sim_candles.append(c)

        # 2. Re-play exact same observations through the IBKRMarketDataAdapter
        client = mock.Mock(spec=TWSClient)
        client.is_connected.return_value = True

        adapter = IBKRMarketDataAdapter(client, Settings())
        req_id = adapter.request_market_data()

        adapter_builder = CandleBuilder(timeframe_minutes=5)
        adapter_processor = MarketDataCandleProcessor(candle_builder=adapter_builder)

        for tick in ticks:
            with mock.patch("app.market_data.ibkr_market_data.datetime") as mock_dt:
                mock_dt.now.return_value = tick.timestamp
                adapter.on_tick_price(req_id, 4, float(tick.price))
                adapter.on_tick_size(req_id, 5, tick.volume)

        # Flush final price
        adapter.cancel_market_data()

        adapter_candles = []
        while adapter.queue_size() > 0:
            event = adapter.get_event()
            assert event is not None
            c = adapter_processor.process_event(event)
            if c is not None:
                adapter_candles.append(c)

        assert len(sim_candles) == len(adapter_candles)
        for c_sim, c_ada in zip(sim_candles, adapter_candles):
            assert c_sim.timestamp == c_ada.timestamp
            assert c_sim.open == c_ada.open
            assert c_sim.high == c_ada.high
            assert c_sim.low == c_ada.low
            assert c_sim.close == c_ada.close
            assert c_sim.volume == c_ada.volume
