"""CandleStrategyProcessor — integrates completed Candle streams with BaseStrategy."""

import logging

from app.models.candle import Candle
from app.models.signal import Signal
from app.strategy.base_strategy import BaseStrategy

logger = logging.getLogger(__name__)


class CandleStrategyProcessor:
    """Orchestrates completed Candle sequence with BaseStrategy to produce signals.

    Maintains the chronological history of completed candles and evaluates the strategy.
    """

    def __init__(self, strategy: BaseStrategy) -> None:
        """Initialize with an injected strategy instance."""
        self._strategy = strategy
        self._candles: list[Candle] = []

    def process_candle(self, candle: Candle) -> Signal:
        """Process a single completed Candle.

        Args:
            candle: The completed Candle.

        Returns:
            The generated Signal (BUY, SELL, or HOLD).
        """
        logger.debug("Strategy processor received completed candle: %s", candle)
        self._candles.append(candle)

        try:
            signal = self._strategy.evaluate(list(self._candles))
            logger.info(
                'Strategy signal: type=%s reason="%s"',
                signal.signal_type.name,
                signal.reason,
            )
            return signal
        except Exception as e:
            logger.error("Strategy evaluation failure: %s", e)
            raise

    def get_candles(self) -> list[Candle]:
        """Return a defensive copy of the internal candle history."""
        return list(self._candles)
