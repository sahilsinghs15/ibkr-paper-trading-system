"""Market-data event simulator for generating deterministic ticks."""

from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal

from app.models.market_data import MarketDataEvent


class MarketDataSimulator:
    """Generates deterministic sequences of MarketDataEvent objects.

    Useful for local testing, development, and executing predictable
    strategy integration checks.

    Responsibility Boundary:
    - This simulator is responsible ONLY for generating raw market-data events
      representing deterministic price movements.
      It does NOT contain candle classification, strategy decision logic,
      or order placement.
    - Mixed patterns are not represented as a single "MIXED" pattern type.
      Instead, they are represented as a sequence list of individual candle
      patterns (e.g., ["BULLISH", "BEARISH", "NEUTRAL"]).
    - The strategy remains responsible for deciding actions (BUY/SELL/HOLD) based
      on completed candle directions.
    """

    def __init__(
        self,
        symbol: str,
        starting_price: Decimal,
        patterns: list[str],
        start_time: datetime,
        ticks_per_candle: int = 5,
        tick_interval_seconds: int = 60,
        volume: int = 100,
    ) -> None:
        """Initialize the simulator with configurations."""
        if not symbol or not symbol.strip():
            raise ValueError("Symbol must be a non-empty string.")
        if starting_price <= 0:
            raise ValueError("Starting price must be positive.")
        if not patterns:
            raise ValueError("Patterns list must be non-empty.")
        if start_time.tzinfo is None:
            raise ValueError("Start time must be timezone-aware.")
        if ticks_per_candle <= 0:
            raise ValueError("Ticks per candle must be positive.")
        if tick_interval_seconds <= 0:
            raise ValueError("Tick interval seconds must be positive.")
        if volume <= 0:
            raise ValueError("Volume must be positive.")

        # Enforce that one pattern represents exactly one 5-minute candle
        if ticks_per_candle * tick_interval_seconds != 300:
            raise ValueError(
                f"ticks_per_candle ({ticks_per_candle}) * tick_interval_seconds ({tick_interval_seconds}) "
                f"must equal 300 seconds (exactly 5 minutes)."
            )

        # Enforce start time boundary alignment
        if (
            start_time.minute % 5 != 0
            or start_time.second != 0
            or start_time.microsecond != 0
        ):
            raise ValueError(
                f"Start time must be aligned to a 5-minute wall-clock boundary (minute % 5 == 0, second == 0, microsecond == 0): "
                f"got {start_time}"
            )

        self._symbol = symbol
        self._starting_price = starting_price
        self._start_time = start_time
        self._ticks_per_candle = ticks_per_candle
        self._tick_interval_seconds = tick_interval_seconds
        self._volume = volume

        self._patterns = []
        for p in patterns:
            p_upper = p.upper()
            if p_upper not in ("BULLISH", "BEARISH", "NEUTRAL"):
                raise ValueError(f"Invalid candle pattern: {p}")
            self._patterns.append(p_upper)

    def iter_events(self) -> Iterator[MarketDataEvent]:
        """Generate and yield a deterministic sequence of MarketDataEvent objects.

        Yields:
            MarketDataEvent objects spaced by tick_interval_seconds.
        """
        current_time = self._start_time
        current_price = self._starting_price

        for pattern in self._patterns:
            for step in range(self._ticks_per_candle):
                if pattern == "BULLISH":
                    price = current_price + Decimal(step + 1)
                elif pattern == "BEARISH":
                    price = current_price - Decimal(step + 1)
                else:  # NEUTRAL
                    price = current_price

                if price <= 0:
                    raise ValueError(f"Generated price became non-positive: {price}")

                yield MarketDataEvent(
                    timestamp=current_time,
                    price=price,
                    volume=self._volume,
                )

                current_time += timedelta(seconds=self._tick_interval_seconds)

            # Advance current close price for the next candle
            if pattern == "BULLISH":
                current_price = current_price + Decimal(self._ticks_per_candle)
            elif pattern == "BEARISH":
                current_price = current_price - Decimal(self._ticks_per_candle)

        # Yield one final event to trigger the completion of the last candle
        yield MarketDataEvent(
            timestamp=current_time,
            price=current_price,
            volume=self._volume,
        )
