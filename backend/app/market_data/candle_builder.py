"""CandleBuilder — converts MarketDataEvent streams into completed Candle objects.

Aggregates individual market-data events into fixed wall-clock OHLCV candles.
The default timeframe is 5 minutes, aligned to wall-clock boundaries
(e.g. 10:00:00–10:04:59, 10:05:00–10:09:59).

Gap behaviour:
    If no events arrive during an interval, NO synthetic candle is created.
    Only intervals that contain at least one event produce a candle.

Event ordering:
    Events must arrive in chronological order.  An event whose timestamp
    is strictly earlier than the latest processed event raises ``ValueError``.
    Events with equal timestamps are valid and processed normally.
"""

import logging
from datetime import datetime
from decimal import Decimal

from app.models.candle import Candle
from app.models.market_data import MarketDataEvent

logger = logging.getLogger(__name__)


class CandleBuilder:
    """Stateful builder that converts a stream of ``MarketDataEvent`` objects
    into completed ``Candle`` objects.

    Args:
        timeframe_minutes: Duration of each candle in minutes.
                           Must be a positive integer.  Defaults to ``5``.
    """

    def __init__(self, timeframe_minutes: int = 5) -> None:
        if timeframe_minutes <= 0:
            raise ValueError(
                f"timeframe_minutes must be positive, got {timeframe_minutes}"
            )
        self._timeframe_minutes: int = timeframe_minutes

        # Current in-progress candle state (None when no events received yet)
        self._interval_start: datetime | None = None
        self._open: Decimal | None = None
        self._high: Decimal | None = None
        self._low: Decimal | None = None
        self._close: Decimal | None = None
        self._volume: int = 0

        # For out-of-order detection
        self._last_event_timestamp: datetime | None = None

    # ── public API ───────────────────────────────────────────────────

    def add_event(self, event: MarketDataEvent) -> Candle | None:
        """Process a market-data event and return a completed candle if one
        has been finalised by this event.

        Returns:
            The completed ``Candle`` when *event* belongs to a **new** interval
            (triggering finalisation of the previous one), or ``None`` if the
            event simply updates the current in-progress candle.
        """
        self._validate_event(event)

        interval_start = self._compute_interval_start(event.timestamp)
        self._last_event_timestamp = event.timestamp

        # First event ever — start a new candle
        if self._interval_start is None:
            self._start_new_candle(interval_start, event)
            return None

        # Event belongs to the current interval — update it
        if interval_start == self._interval_start:
            self._update_current_candle(event)
            return None

        # Event belongs to a new interval — finalise the old one
        completed = self._finalise_candle()
        self._start_new_candle(interval_start, event)
        return completed

    # ── private helpers ──────────────────────────────────────────────

    def _validate_event(self, event: MarketDataEvent) -> None:
        """Reject naive timestamps and out-of-order events."""
        if event.timestamp.tzinfo is None:
            raise ValueError(
                "MarketDataEvent timestamp must be timezone-aware, "
                f"got naive datetime: {event.timestamp}"
            )
        if (
            self._last_event_timestamp is not None
            and event.timestamp < self._last_event_timestamp
        ):
            raise ValueError(
                f"Out-of-order event: received {event.timestamp} "
                f"but latest processed was {self._last_event_timestamp}"
            )

    def _compute_interval_start(self, ts: datetime) -> datetime:
        """Floor *ts* to the nearest candle boundary."""
        total_minutes = ts.hour * 60 + ts.minute
        floored_minutes = (
            total_minutes // self._timeframe_minutes
        ) * self._timeframe_minutes
        return ts.replace(
            hour=floored_minutes // 60,
            minute=floored_minutes % 60,
            second=0,
            microsecond=0,
        )

    def _start_new_candle(
        self, interval_start: datetime, event: MarketDataEvent
    ) -> None:
        """Initialise a fresh in-progress candle from the given event."""
        self._interval_start = interval_start
        self._open = event.price
        self._high = event.price
        self._low = event.price
        self._close = event.price
        self._volume = event.volume

    def _update_current_candle(self, event: MarketDataEvent) -> None:
        """Update the in-progress candle with a new event."""
        assert self._high is not None and self._low is not None
        self._high = max(self._high, event.price)
        self._low = min(self._low, event.price)
        self._close = event.price
        self._volume += event.volume

    def _finalise_candle(self) -> Candle:
        """Create a completed ``Candle`` from the current in-progress state."""
        assert (
            self._interval_start is not None
            and self._open is not None
            and self._high is not None
            and self._low is not None
            and self._close is not None
        )
        candle = Candle(
            timestamp=self._interval_start,
            open=self._open,
            high=self._high,
            low=self._low,
            close=self._close,
            volume=self._volume,
        )
        logger.info(
            "Candle completed: timestamp=%s open=%s high=%s low=%s close=%s volume=%d",
            candle.timestamp,
            candle.open,
            candle.high,
            candle.low,
            candle.close,
            candle.volume,
        )
        return candle
