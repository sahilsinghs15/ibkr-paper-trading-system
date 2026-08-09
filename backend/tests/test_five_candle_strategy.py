"""Tests for FiveCandleStrategy — Phase 2.3.

Verifies observable strategy behaviour through the public ``evaluate`` API.
All candles use deterministic data; no sleep, network, or system clock usage.
"""

from datetime import UTC, datetime
from decimal import Decimal

from app.models.candle import Candle
from app.models.signal import SignalType
from app.strategy.five_candle_strategy import FiveCandleStrategy

# ── helpers ──────────────────────────────────────────────────────────


def _bullish(minute: int = 0) -> Candle:
    """Create a bullish candle (close > open)."""
    return Candle(
        timestamp=datetime(2025, 6, 15, 10, minute, 0, tzinfo=UTC),
        open=Decimal(100),
        high=Decimal(110),
        low=Decimal(99),
        close=Decimal(108),
        volume=1000,
    )


def _bearish(minute: int = 0) -> Candle:
    """Create a bearish candle (close < open)."""
    return Candle(
        timestamp=datetime(2025, 6, 15, 10, minute, 0, tzinfo=UTC),
        open=Decimal(108),
        high=Decimal(110),
        low=Decimal(99),
        close=Decimal(100),
        volume=1000,
    )


def _neutral(minute: int = 0) -> Candle:
    """Create a neutral candle (close == open)."""
    return Candle(
        timestamp=datetime(2025, 6, 15, 10, minute, 0, tzinfo=UTC),
        open=Decimal(100),
        high=Decimal(110),
        low=Decimal(90),
        close=Decimal(100),
        volume=1000,
    )


# ── Insufficient candles ────────────────────────────────────────────


class TestInsufficientCandles:
    """Tests 1-3: fewer than 5 candles → HOLD."""

    def test_empty_list_returns_hold(self) -> None:
        strategy = FiveCandleStrategy()
        signal = strategy.evaluate([])
        assert signal.signal_type == SignalType.HOLD

    def test_one_candle_returns_hold(self) -> None:
        strategy = FiveCandleStrategy()
        signal = strategy.evaluate([_bullish()])
        assert signal.signal_type == SignalType.HOLD

    def test_four_candles_returns_hold(self) -> None:
        strategy = FiveCandleStrategy()
        signal = strategy.evaluate([_bullish(i * 5) for i in range(4)])
        assert signal.signal_type == SignalType.HOLD


# ── All bullish ─────────────────────────────────────────────────────


class TestAllBullish:
    """Tests 4-5: five bullish candles → BUY."""

    def test_exactly_five_bullish_returns_buy(self) -> None:
        strategy = FiveCandleStrategy()
        candles = [_bullish(i * 5) for i in range(5)]
        signal = strategy.evaluate(candles)
        assert signal.signal_type == SignalType.BUY

    def test_more_than_five_with_latest_five_bullish_returns_buy(self) -> None:
        """Older mixed candles must not influence the result."""
        strategy = FiveCandleStrategy()
        older = [_bearish(0), _neutral(5), _bearish(10)]
        latest_five = [_bullish(i * 5 + 15) for i in range(5)]
        signal = strategy.evaluate(older + latest_five)
        assert signal.signal_type == SignalType.BUY


# ── All bearish ─────────────────────────────────────────────────────


class TestAllBearish:
    """Tests 6-7: five bearish candles → SELL."""

    def test_exactly_five_bearish_returns_sell(self) -> None:
        strategy = FiveCandleStrategy()
        candles = [_bearish(i * 5) for i in range(5)]
        signal = strategy.evaluate(candles)
        assert signal.signal_type == SignalType.SELL

    def test_more_than_five_with_latest_five_bearish_returns_sell(self) -> None:
        """Older mixed candles must not influence the result."""
        strategy = FiveCandleStrategy()
        older = [_bullish(0), _neutral(5), _bullish(10)]
        latest_five = [_bearish(i * 5 + 15) for i in range(5)]
        signal = strategy.evaluate(older + latest_five)
        assert signal.signal_type == SignalType.SELL


# ── Mixed ───────────────────────────────────────────────────────────


class TestMixed:
    """Tests 8-11: mixed direction sequences → HOLD."""

    def test_four_bullish_one_bearish_returns_hold(self) -> None:
        strategy = FiveCandleStrategy()
        candles = [_bullish(0), _bullish(5), _bullish(10), _bullish(15), _bearish(20)]
        signal = strategy.evaluate(candles)
        assert signal.signal_type == SignalType.HOLD

    def test_four_bearish_one_bullish_returns_hold(self) -> None:
        strategy = FiveCandleStrategy()
        candles = [_bearish(0), _bearish(5), _bearish(10), _bearish(15), _bullish(20)]
        signal = strategy.evaluate(candles)
        assert signal.signal_type == SignalType.HOLD

    def test_alternating_returns_hold(self) -> None:
        strategy = FiveCandleStrategy()
        candles = [
            _bullish(0),
            _bearish(5),
            _bullish(10),
            _bearish(15),
            _bullish(20),
        ]
        signal = strategy.evaluate(candles)
        assert signal.signal_type == SignalType.HOLD

    def test_arbitrary_mixed_sequence_returns_hold(self) -> None:
        strategy = FiveCandleStrategy()
        candles = [
            _bearish(0),
            _bullish(5),
            _bullish(10),
            _bearish(15),
            _bullish(20),
        ]
        signal = strategy.evaluate(candles)
        assert signal.signal_type == SignalType.HOLD


# ── Neutral ─────────────────────────────────────────────────────────


class TestNeutral:
    """Tests 12-15: any neutral candle in the sequence → HOLD."""

    def test_one_neutral_among_five_returns_hold(self) -> None:
        strategy = FiveCandleStrategy()
        candles = [
            _bullish(0),
            _bullish(5),
            _neutral(10),
            _bullish(15),
            _bullish(20),
        ]
        signal = strategy.evaluate(candles)
        assert signal.signal_type == SignalType.HOLD

    def test_all_five_neutral_returns_hold(self) -> None:
        strategy = FiveCandleStrategy()
        candles = [_neutral(i * 5) for i in range(5)]
        signal = strategy.evaluate(candles)
        assert signal.signal_type == SignalType.HOLD

    def test_four_bullish_one_neutral_returns_hold(self) -> None:
        strategy = FiveCandleStrategy()
        candles = [_bullish(0), _bullish(5), _bullish(10), _bullish(15), _neutral(20)]
        signal = strategy.evaluate(candles)
        assert signal.signal_type == SignalType.HOLD

    def test_four_bearish_one_neutral_returns_hold(self) -> None:
        strategy = FiveCandleStrategy()
        candles = [_bearish(0), _bearish(5), _bearish(10), _bearish(15), _neutral(20)]
        signal = strategy.evaluate(candles)
        assert signal.signal_type == SignalType.HOLD


# ── Latest five only ────────────────────────────────────────────────


class TestLatestFiveOnly:
    """Tests 16-17: older candles must not influence the result."""

    def test_older_bearish_does_not_prevent_buy(self) -> None:
        """[BEAR, BEAR, BULL, BULL, BULL, BULL, BULL] → BUY."""
        strategy = FiveCandleStrategy()
        candles = [_bearish(0), _bearish(5)] + [_bullish(i * 5 + 10) for i in range(5)]
        signal = strategy.evaluate(candles)
        assert signal.signal_type == SignalType.BUY

    def test_older_bullish_does_not_prevent_sell(self) -> None:
        """[BULL, BULL, BEAR, BEAR, BEAR, BEAR, BEAR] → SELL."""
        strategy = FiveCandleStrategy()
        candles = [_bullish(0), _bullish(5)] + [_bearish(i * 5 + 10) for i in range(5)]
        signal = strategy.evaluate(candles)
        assert signal.signal_type == SignalType.SELL


# ── Candle direction classification ─────────────────────────────────


class TestCandleDirection:
    """Tests 18-20: candle direction classification matches spec."""

    def test_close_greater_than_open_is_bullish(self) -> None:
        candle = Candle(
            timestamp=datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC),
            open=Decimal(100),
            high=Decimal(110),
            low=Decimal(99),
            close=Decimal(101),
            volume=100,
        )
        assert candle.is_bullish is True
        assert candle.is_bearish is False

    def test_close_less_than_open_is_bearish(self) -> None:
        candle = Candle(
            timestamp=datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC),
            open=Decimal(100),
            high=Decimal(110),
            low=Decimal(99),
            close=Decimal(99),
            volume=100,
        )
        assert candle.is_bullish is False
        assert candle.is_bearish is True

    def test_close_equals_open_is_neutral(self) -> None:
        candle = Candle(
            timestamp=datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC),
            open=Decimal(100),
            high=Decimal(110),
            low=Decimal(90),
            close=Decimal(100),
            volume=100,
        )
        assert candle.is_bullish is False
        assert candle.is_bearish is False


# ── Input immutability ──────────────────────────────────────────────


class TestInputImmutability:
    """Test 21: strategy must not mutate the input list."""

    def test_evaluate_does_not_mutate_input_list(self) -> None:
        strategy = FiveCandleStrategy()
        candles = [_bullish(i * 5) for i in range(7)]
        original = list(candles)

        strategy.evaluate(candles)

        assert candles == original
        assert len(candles) == len(original)
