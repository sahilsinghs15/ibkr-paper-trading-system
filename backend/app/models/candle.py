"""Candle domain model representing a completed market candle."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class Candle:
    """A completed OHLCV market candle.

    Uses Decimal for price fields to avoid floating-point precision issues
    in financial calculations.
    """

    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    @property
    def is_bullish(self) -> bool:
        """A candle is bullish when close is strictly greater than open."""
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        """A candle is bearish when close is strictly less than open."""
        return self.close < self.open
