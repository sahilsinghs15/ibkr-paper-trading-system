"""Five-candle trading strategy.

Evaluates the latest five completed candles and produces a signal:

    5 bullish → BUY
    5 bearish → SELL
    otherwise → HOLD

A candle is bullish when ``close > open`` and bearish when
``close < open``.  A neutral candle (``close == open``) causes HOLD.
"""

import logging
from datetime import UTC, datetime

from app.models.candle import Candle
from app.models.signal import Signal, SignalType
from app.strategy.base_strategy import BaseStrategy

logger = logging.getLogger(__name__)

_REQUIRED_CANDLES = 5


class FiveCandleStrategy(BaseStrategy):
    """Trading strategy based on five consecutive candle directions.

    The strategy inspects only the **latest five** completed candles
    (``candles[-5:]``).  If all five are bullish the signal is BUY;
    if all five are bearish the signal is SELL; every other case
    (mixed directions, neutral candles, or fewer than five candles)
    results in HOLD.
    """

    def evaluate(self, candles: list[Candle]) -> Signal:
        """Evaluate the latest five candles and return a trading signal.

        Args:
            candles: Completed candles in chronological order
                     (oldest → newest).

        Returns:
            A ``Signal`` with type BUY, SELL, or HOLD.
        """
        now = datetime.now(UTC)

        if len(candles) < _REQUIRED_CANDLES:
            reason = f"Insufficient candles: {len(candles)}/{_REQUIRED_CANDLES}"
            return Signal(signal_type=SignalType.HOLD, timestamp=now, reason=reason)

        latest = candles[-_REQUIRED_CANDLES:]

        if all(c.is_bullish for c in latest):
            signal_type = SignalType.BUY
            reason = "Five consecutive bullish candles"
        elif all(c.is_bearish for c in latest):
            signal_type = SignalType.SELL
            reason = "Five consecutive bearish candles"
        else:
            signal_type = SignalType.HOLD
            reason = "Mixed or neutral candle sequence"

        logger.info("FiveCandleStrategy signal=%s reason=%s", signal_type.value, reason)
        return Signal(signal_type=signal_type, timestamp=now, reason=reason)
