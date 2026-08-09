"""MarketDataCandleProcessor — processes MarketDataEvent streams into Candle objects."""

import logging

from app.market_data.candle_builder import CandleBuilder
from app.models.candle import Candle
from app.models.market_data import MarketDataEvent

logger = logging.getLogger(__name__)


class MarketDataCandleProcessor:
    """Component responsible for consuming MarketDataEvent objects

    and forwarding them to CandleBuilder.
    """

    def __init__(self, candle_builder: CandleBuilder) -> None:
        """Initialize with an injected CandleBuilder instance."""
        self._candle_builder = candle_builder

    def process_event(self, event: MarketDataEvent) -> Candle | None:
        """Process a single MarketDataEvent.

        Forwards the event to the injected CandleBuilder.

        Args:
            event: The timezone-aware normalized MarketDataEvent to process.

        Returns:
            A completed Candle if finalized by this event, otherwise None.

        Raises:
            ValueError: If the event has a naive timestamp or is out of order.
        """
        logger.debug("Processor received event: %s", event)
        try:
            candle = self._candle_builder.add_event(event)
            if candle is not None:
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
        except ValueError as e:
            logger.error("CandleBuilder processing failure: %s", e)
            raise
