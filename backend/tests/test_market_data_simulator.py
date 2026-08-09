"""Tests for MarketDataSimulator — Phase 2.7."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.market_data.candle_builder import CandleBuilder
from app.market_data.simulator import MarketDataSimulator
from app.models.candle import Candle
from app.models.market_data import MarketDataEvent
from app.models.signal import SignalType
from app.strategy.five_candle_strategy import FiveCandleStrategy


def _simulator(
    patterns: list[str],
    ticks_per_candle: int = 5,
    tick_interval_seconds: int = 60,
    start_time: datetime | None = None,
) -> MarketDataSimulator:
    if start_time is None:
        start_time = datetime(2025, 6, 15, 10, 0, tzinfo=UTC)
    return MarketDataSimulator(
        symbol="RELIANCE",
        starting_price=Decimal("100.00"),
        patterns=patterns,
        start_time=start_time,
        ticks_per_candle=ticks_per_candle,
        tick_interval_seconds=tick_interval_seconds,
        volume=100,
    )


def _build_candles(
    patterns: list[str],
    ticks_per_candle: int = 5,
    tick_interval_seconds: int = 60,
    start_time: datetime | None = None,
) -> list[Candle]:
    """Test helper that runs simulator ticks through real CandleBuilder."""
    sim = _simulator(patterns, ticks_per_candle, tick_interval_seconds, start_time)
    builder = CandleBuilder(timeframe_minutes=5)
    candles = []
    for e in sim.iter_events():
        c = builder.add_event(e)
        if c is not None:
            candles.append(c)
    return candles


class TestMarketDataSimulator:
    def test_valid_default_configuration(self) -> None:
        """Verify that default settings of 5 ticks and 60 seconds interval is

        valid.
        """
        sim = _simulator(["BULLISH"])
        assert sim._symbol == "RELIANCE"
        assert sim._starting_price == Decimal("100.00")
        assert sim._ticks_per_candle == 5
        assert sim._tick_interval_seconds == 60
        assert sim._volume == 100
        assert sim._patterns == ["BULLISH"]

    def test_basic_generation(self) -> None:
        """Verify basic generator properties (event count, types, volume,

        timezones, monotonicity).
        """
        sim = _simulator(["BULLISH", "BEARISH"])
        events = list(sim.iter_events())

        # Total events = M * ticks_per_candle + 1
        assert len(events) == 2 * 5 + 1

        # Every event is a MarketDataEvent
        for e in events:
            assert isinstance(e, MarketDataEvent)

        # Timestamps are timezone-aware
        for e in events:
            assert e.timestamp.tzinfo is not None

        # Timestamps are monotonically increasing
        for i in range(1, len(events)):
            assert events[i].timestamp > events[i - 1].timestamp

        # Generated volume matches configuration
        for e in events:
            assert e.volume == 100

    def test_determinism(self) -> None:
        """Running the simulator twice with same config produces identical

        events.
        """
        sim1 = _simulator(["BULLISH", "BEARISH"])
        sim2 = _simulator(["BULLISH", "BEARISH"])
        events1 = list(sim1.iter_events())
        events2 = list(sim2.iter_events())
        assert events1 == events2

    def test_price_behavior(self) -> None:
        """Generated prices are positive Decimals."""
        sim = _simulator(["BULLISH"])
        events = list(sim.iter_events())

        for e in events:
            assert isinstance(e.price, Decimal)
            assert e.price > 0

    def test_ohlc_relationships_and_candle_types(self) -> None:
        """Generated patterns produce valid OHLC relationships and expected

        candle directions when passed through real CandleBuilder.
        """
        # Bullish pattern produces bullish candle
        candles_bull = _build_candles(["BULLISH"])
        assert len(candles_bull) == 1
        c = candles_bull[0]
        assert c.high >= max(c.open, c.close)
        assert c.low <= min(c.open, c.close)
        assert c.is_bullish is True
        assert c.is_bearish is False

        # Bearish pattern produces bearish candle
        candles_bear = _build_candles(["BEARISH"])
        assert len(candles_bear) == 1
        c = candles_bear[0]
        assert c.high >= max(c.open, c.close)
        assert c.low <= min(c.open, c.close)
        assert c.is_bullish is False
        assert c.is_bearish is True

        # Neutral pattern produces neutral candle
        candles_neutral = _build_candles(["NEUTRAL"])
        assert len(candles_neutral) == 1
        c = candles_neutral[0]
        assert c.high >= max(c.open, c.close)
        assert c.low <= min(c.open, c.close)
        assert c.is_bullish is False
        assert c.is_bearish is False

        # Mixed sequence produces expected candle directions
        candles_mixed = _build_candles(["BULLISH", "BEARISH", "NEUTRAL"])
        assert len(candles_mixed) == 3
        assert candles_mixed[0].is_bullish is True
        assert candles_mixed[1].is_bearish is True
        assert candles_mixed[2].is_bullish is False
        assert candles_mixed[2].is_bearish is False

    def test_five_candle_strategy_integration(self) -> None:
        """Generated pattern sequences yield correct strategy signals."""
        strategy = FiveCandleStrategy()

        # 5 bullish candles yield BUY
        candles_buy = _build_candles(["BULLISH"] * 5)
        assert len(candles_buy) == 5
        signal_buy = strategy.evaluate(candles_buy)
        assert signal_buy.signal_type == SignalType.BUY

        # 5 bearish candles yield SELL
        candles_sell = _build_candles(["BEARISH"] * 5)
        assert len(candles_sell) == 5
        signal_sell = strategy.evaluate(candles_sell)
        assert signal_sell.signal_type == SignalType.SELL

        # Mixed five-candle sequence yields HOLD
        candles_hold = _build_candles(
            ["BULLISH", "BEARISH", "BULLISH", "BEARISH", "NEUTRAL"]
        )
        assert len(candles_hold) == 5
        signal_hold = strategy.evaluate(candles_hold)
        assert signal_hold.signal_type == SignalType.HOLD

        # Neutral candle yields HOLD (insufficient count or neutral type)
        candles_single_neutral = _build_candles(["NEUTRAL"])
        assert len(candles_single_neutral) == 1
        signal_single_neutral = strategy.evaluate(candles_single_neutral)
        assert signal_single_neutral.signal_type == SignalType.HOLD

    def test_timeframe_validation(self) -> None:
        """Simulator rejects combinations that do not represent exactly 5

        minutes (300 seconds).
        """
        # Reject 3 ticks at 60s
        with pytest.raises(
            ValueError,
            match="must equal 300 seconds",
        ):
            _simulator(["BULLISH"], ticks_per_candle=3, tick_interval_seconds=60)

        # Reject 5 ticks at 30s
        with pytest.raises(
            ValueError,
            match="must equal 300 seconds",
        ):
            _simulator(["BULLISH"], ticks_per_candle=5, tick_interval_seconds=30)

        # Accept valid alternative: 10 ticks at 30s
        sim = _simulator(["BULLISH"], ticks_per_candle=10, tick_interval_seconds=30)
        assert sim._ticks_per_candle == 10
        assert sim._tick_interval_seconds == 30

    def test_start_time_alignment_validation(self) -> None:
        """Simulator rejects start time that is not aligned to a 5-minute

        boundary.
        """
        # Reject 10:02:00
        with pytest.raises(
            ValueError,
            match="must be aligned to a 5-minute wall-clock boundary",
        ):
            _simulator(
                ["BULLISH"], start_time=datetime(2025, 6, 15, 10, 2, 0, tzinfo=UTC)
            )

        # Reject 10:05:01
        with pytest.raises(
            ValueError,
            match="must be aligned to a 5-minute wall-clock boundary",
        ):
            _simulator(
                ["BULLISH"], start_time=datetime(2025, 6, 15, 10, 5, 1, tzinfo=UTC)
            )

        # Reject 10:05:00.123456 (microsecond)
        with pytest.raises(
            ValueError,
            match="must be aligned to a 5-minute wall-clock boundary",
        ):
            _simulator(
                ["BULLISH"],
                start_time=datetime(2025, 6, 15, 10, 5, 0, 123456, tzinfo=UTC),
            )

        # Accept valid timestamps
        valid_times = [
            datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC),
            datetime(2025, 6, 15, 10, 5, 0, tzinfo=UTC),
            datetime(2025, 6, 15, 10, 10, 0, tzinfo=UTC),
        ]
        for t in valid_times:
            sim = _simulator(["BULLISH"], start_time=t)
            assert sim._start_time == t

    def test_validation_edge_cases(self) -> None:
        """Verify additional validation and error raising for bad inputs."""
        # Zero events/patterns
        with pytest.raises(ValueError, match="Patterns list must be non-empty."):
            MarketDataSimulator("AAPL", Decimal(100), [], datetime.now(UTC))

        # Invalid starting price
        with pytest.raises(ValueError, match="Starting price must be positive."):
            MarketDataSimulator(
                "AAPL",
                Decimal("-10.00"),
                ["BULLISH"],
                datetime(2025, 6, 15, 10, 0, tzinfo=UTC),
            )

        # Invalid symbol
        with pytest.raises(ValueError, match="Symbol must be a non-empty string."):
            MarketDataSimulator(
                "",
                Decimal("100.00"),
                ["BULLISH"],
                datetime(2025, 6, 15, 10, 0, tzinfo=UTC),
            )

        # Naive timestamp
        with pytest.raises(ValueError, match="Start time must be timezone-aware."):
            MarketDataSimulator(
                "AAPL",
                Decimal("100.00"),
                ["BULLISH"],
                datetime(2025, 6, 15, 10, 0),  # noqa: DTZ001
            )

        # Invalid patterns
        with pytest.raises(ValueError, match="Invalid candle pattern"):
            MarketDataSimulator(
                "AAPL",
                Decimal("100.00"),
                ["INVALID"],
                datetime(2025, 6, 15, 10, 0, tzinfo=UTC),
            )

        # Invalid ticks_per_candle (0 or negative, which fails ticks * interval == 300)
        with pytest.raises(ValueError, match="Ticks per candle must be positive."):
            MarketDataSimulator(
                "AAPL",
                Decimal("100.00"),
                ["BULLISH"],
                datetime(2025, 6, 15, 10, 0, tzinfo=UTC),
                ticks_per_candle=0,
            )

        # Invalid tick_interval_seconds (0 or negative)
        with pytest.raises(ValueError, match="Tick interval seconds must be positive."):
            MarketDataSimulator(
                "AAPL",
                Decimal("100.00"),
                ["BULLISH"],
                datetime(2025, 6, 15, 10, 0, tzinfo=UTC),
                tick_interval_seconds=0,
            )

        # Invalid volume
        with pytest.raises(ValueError, match="Volume must be positive."):
            MarketDataSimulator(
                "AAPL",
                Decimal("100.00"),
                ["BULLISH"],
                datetime(2025, 6, 15, 10, 0, tzinfo=UTC),
                volume=-5,
            )

        # Price goes below zero (bearish ticks drop below 0)
        sim = MarketDataSimulator(
            "AAPL",
            Decimal(1),
            ["BEARISH"],
            datetime(2025, 6, 15, 10, 0, tzinfo=UTC),
            ticks_per_candle=5,
        )
        with pytest.raises(ValueError, match="Generated price became non-positive"):
            list(sim.iter_events())
