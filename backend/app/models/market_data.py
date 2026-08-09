"""Market data event domain model representing an incoming price update."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class MarketDataEvent:
    """A single market-data price update.

    Represents an incoming tick or price snapshot.  This model is
    broker-agnostic and contains only the fields needed for candle
    aggregation.

    Attributes:
        timestamp: When the price was observed (must be timezone-aware).
        price: The observed price.
        volume: The volume associated with this update.
    """

    timestamp: datetime
    price: Decimal
    volume: int

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError(
                "MarketDataEvent requires a timezone-aware timestamp, "
                f"got naive datetime: {self.timestamp}"
            )
