"""Abstract base strategy interface.

Defines the contract that all trading strategies must satisfy.
The strategy receives completed candles and returns a trading signal.
"""

from abc import ABC, abstractmethod

from app.models.candle import Candle
from app.models.signal import Signal


class BaseStrategy(ABC):
    """Abstract trading strategy.

    Subclasses implement ``evaluate`` to analyze candles and produce
    a trading signal.  The interface is intentionally minimal and
    independent of infrastructure concerns.
    """

    @abstractmethod
    def evaluate(self, candles: list[Candle]) -> Signal:
        """Evaluate a sequence of completed candles and return a signal.

        Args:
            candles: Completed candles in chronological order.
                     The list length depends on the strategy's requirements.

        Returns:
            A Signal indicating the recommended action.
        """
