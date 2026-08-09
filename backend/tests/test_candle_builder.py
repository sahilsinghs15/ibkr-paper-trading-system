"""Tests for CandleBuilder — Phase 2.2.

Verifies observable behaviour through the public ``add_event`` API.
All timestamps are deterministic; no sleep, network, or system clock usage.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.market_data.candle_builder import CandleBuilder
from app.models.candle import Candle
from app.models.market_data import MarketDataEvent

# ── helpers ──────────────────────────────────────────────────────────


def _event(
    hour: int,
    minute: int,
    second: int,
    price: str,
    volume: int = 100,
) -> MarketDataEvent:
    """Create a MarketDataEvent with a concise call-site."""
    return MarketDataEvent(
        timestamp=datetime(2025, 6, 15, hour, minute, second, tzinfo=UTC),
        price=Decimal(price),
        volume=volume,
    )


# ── Basic ────────────────────────────────────────────────────────────


class TestBasicBehaviour:
    """Tests 1-3: first event, same-interval, new-interval."""

    def test_first_event_returns_none(self) -> None:
        builder = CandleBuilder()
        result = builder.add_event(_event(10, 0, 5, "100"))
        assert result is None

    def test_event_within_same_interval_returns_none(self) -> None:
        builder = CandleBuilder()
        builder.add_event(_event(10, 0, 5, "100"))
        result = builder.add_event(_event(10, 1, 10, "102"))
        assert result is None

    def test_new_interval_emits_previous_candle(self) -> None:
        builder = CandleBuilder()
        builder.add_event(_event(10, 0, 5, "100"))
        completed = builder.add_event(_event(10, 5, 0, "105"))

        assert completed is not None
        assert completed.timestamp == datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC)


# ── OHLCV ────────────────────────────────────────────────────────────


class TestOHLCV:
    """Tests 4-8: correct open, high, low, close, volume."""

    def _build_candle(self) -> tuple[CandleBuilder, Candle | None]:
        builder = CandleBuilder()
        builder.add_event(_event(10, 0, 5, "100", volume=10))
        builder.add_event(_event(10, 1, 10, "102", volume=20))
        builder.add_event(_event(10, 3, 20, "99", volume=30))
        builder.add_event(_event(10, 4, 50, "101", volume=40))
        # Trigger completion
        completed = builder.add_event(_event(10, 5, 0, "105", volume=50))
        return builder, completed

    def test_correct_open(self) -> None:
        _, candle = self._build_candle()
        assert candle is not None
        assert candle.open == Decimal(100)

    def test_correct_high(self) -> None:
        _, candle = self._build_candle()
        assert candle is not None
        assert candle.high == Decimal(102)

    def test_correct_low(self) -> None:
        _, candle = self._build_candle()
        assert candle is not None
        assert candle.low == Decimal(99)

    def test_correct_close(self) -> None:
        _, candle = self._build_candle()
        assert candle is not None
        assert candle.close == Decimal(101)

    def test_correct_accumulated_volume(self) -> None:
        _, candle = self._build_candle()
        assert candle is not None
        assert candle.volume == 10 + 20 + 30 + 40


# ── Boundaries ───────────────────────────────────────────────────────


class TestBoundaries:
    """Tests 9-12: wall-clock boundary alignment."""

    def test_event_at_interval_start_belongs_to_that_interval(self) -> None:
        """10:00:00 belongs to the 10:00 candle."""
        builder = CandleBuilder()
        builder.add_event(_event(10, 0, 0, "100"))
        completed = builder.add_event(_event(10, 5, 0, "105"))

        assert completed is not None
        assert completed.timestamp == datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC)

    def test_event_at_interval_end_belongs_to_that_interval(self) -> None:
        """10:04:59 belongs to the 10:00 candle."""
        builder = CandleBuilder()
        builder.add_event(_event(10, 0, 0, "100"))
        builder.add_event(_event(10, 4, 59, "102"))
        completed = builder.add_event(_event(10, 5, 0, "105"))

        assert completed is not None
        assert completed.timestamp == datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC)
        assert completed.close == Decimal(102)

    def test_event_at_next_boundary_starts_new_candle(self) -> None:
        """10:05:00 starts the 10:05 candle."""
        builder = CandleBuilder()
        builder.add_event(_event(10, 0, 0, "100"))
        completed = builder.add_event(_event(10, 5, 0, "105"))

        assert completed is not None
        # The completed candle is 10:00, not 10:05
        assert completed.timestamp == datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC)

        # Now trigger the 10:05 candle completion
        completed_2 = builder.add_event(_event(10, 10, 0, "110"))
        assert completed_2 is not None
        assert completed_2.timestamp == datetime(2025, 6, 15, 10, 5, 0, tzinfo=UTC)
        assert completed_2.open == Decimal(105)

    def test_boundary_transition_emits_previous_candle_exactly_once(self) -> None:
        builder = CandleBuilder()
        builder.add_event(_event(10, 0, 0, "100"))

        completed = builder.add_event(_event(10, 5, 0, "105"))
        assert completed is not None

        # Additional events in the new interval do NOT re-emit the old candle
        result = builder.add_event(_event(10, 6, 0, "106"))
        assert result is None


# ── Gaps ─────────────────────────────────────────────────────────────


class TestGaps:
    """Test 13: missing intervals do not produce synthetic candles."""

    def test_gap_does_not_produce_synthetic_candles(self) -> None:
        """Event at 10:00, then 10:15 — only the 10:00 candle is emitted."""
        builder = CandleBuilder()
        builder.add_event(_event(10, 0, 0, "100"))

        # Jump to 10:15 — skipping 10:05 and 10:10 entirely
        completed = builder.add_event(_event(10, 15, 0, "115"))

        assert completed is not None
        assert completed.timestamp == datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC)
        assert completed.open == Decimal(100)

        # Exactly one candle was emitted (the 10:00 one).
        # The 10:15 candle is now in progress and not yet returned.
        result = builder.add_event(_event(10, 16, 0, "116"))
        assert result is None


# ── Ordering ─────────────────────────────────────────────────────────


class TestOrdering:
    """Tests 14-15: out-of-order rejection, equal timestamps accepted."""

    def test_out_of_order_event_is_rejected(self) -> None:
        builder = CandleBuilder()
        builder.add_event(_event(10, 1, 0, "100"))

        with pytest.raises(ValueError, match="Out-of-order"):
            builder.add_event(_event(10, 0, 0, "99"))

    def test_equal_timestamps_are_accepted(self) -> None:
        builder = CandleBuilder()
        builder.add_event(_event(10, 0, 0, "100", volume=10))
        result = builder.add_event(_event(10, 0, 0, "102", volume=20))
        assert result is None

        # Trigger completion to verify both events were aggregated
        completed = builder.add_event(_event(10, 5, 0, "105"))
        assert completed is not None
        assert completed.open == Decimal(100)
        assert completed.close == Decimal(102)
        assert completed.volume == 10 + 20


# ── Timezone ─────────────────────────────────────────────────────────


class TestTimezone:
    """Tests 16-17: naive timestamps rejected, timezone-aware accepted."""

    def test_naive_timestamp_rejected_by_model(self) -> None:
        """MarketDataEvent itself rejects naive timestamps."""
        with pytest.raises(ValueError, match="timezone-aware"):
            MarketDataEvent(
                timestamp=datetime(2025, 6, 15, 10, 0, 0),  # noqa: DTZ001
                price=Decimal(100),
                volume=100,
            )

    def test_timezone_aware_timestamps_work_correctly(self) -> None:
        """Timezone-aware timestamps are processed without error."""
        ist = UTC  # Use UTC for determinism
        builder = CandleBuilder()
        event = MarketDataEvent(
            timestamp=datetime(2025, 6, 15, 10, 0, 0, tzinfo=ist),
            price=Decimal(100),
            volume=100,
        )
        result = builder.add_event(event)
        assert result is None


# ── Multiple candles ─────────────────────────────────────────────────


class TestMultipleCandles:
    """Test 18: several consecutive 5-minute candles are built correctly."""

    def test_three_consecutive_candles(self) -> None:
        builder = CandleBuilder()

        # 10:00 candle
        builder.add_event(_event(10, 0, 0, "100", volume=10))
        builder.add_event(_event(10, 2, 0, "103", volume=20))

        # 10:05 candle — triggers 10:00 completion
        candle_1 = builder.add_event(_event(10, 5, 0, "105", volume=30))
        assert candle_1 is not None
        assert candle_1.timestamp == datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC)
        assert candle_1.open == Decimal(100)
        assert candle_1.close == Decimal(103)
        assert candle_1.volume == 30

        builder.add_event(_event(10, 7, 0, "107", volume=40))

        # 10:10 candle — triggers 10:05 completion
        candle_2 = builder.add_event(_event(10, 10, 0, "110", volume=50))
        assert candle_2 is not None
        assert candle_2.timestamp == datetime(2025, 6, 15, 10, 5, 0, tzinfo=UTC)
        assert candle_2.open == Decimal(105)
        assert candle_2.close == Decimal(107)
        assert candle_2.volume == 30 + 40

        builder.add_event(_event(10, 12, 0, "112", volume=60))

        # 10:15 candle — triggers 10:10 completion
        candle_3 = builder.add_event(_event(10, 15, 0, "115", volume=70))
        assert candle_3 is not None
        assert candle_3.timestamp == datetime(2025, 6, 15, 10, 10, 0, tzinfo=UTC)
        assert candle_3.open == Decimal(110)
        assert candle_3.close == Decimal(112)
        assert candle_3.volume == 50 + 60


# ── Configuration ────────────────────────────────────────────────────


class TestConfiguration:
    """Tests 19-20: configurable timeframe, invalid values rejected."""

    def test_custom_timeframe_works(self) -> None:
        """A 15-minute timeframe groups events correctly."""
        builder = CandleBuilder(timeframe_minutes=15)

        builder.add_event(_event(10, 0, 0, "100", volume=10))
        builder.add_event(_event(10, 7, 0, "107", volume=20))
        builder.add_event(_event(10, 14, 59, "114", volume=30))

        # Still within 10:00–10:14 → None
        assert builder.add_event(_event(10, 14, 59, "114", volume=5)) is None

        # 10:15 starts new interval → emits 10:00 candle
        completed = builder.add_event(_event(10, 15, 0, "115", volume=40))
        assert completed is not None
        assert completed.timestamp == datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC)
        assert completed.open == Decimal(100)
        assert completed.close == Decimal(114)
        assert completed.volume == 10 + 20 + 30 + 5

    def test_invalid_timeframe_zero_rejected(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            CandleBuilder(timeframe_minutes=0)

    def test_invalid_timeframe_negative_rejected(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            CandleBuilder(timeframe_minutes=-5)
