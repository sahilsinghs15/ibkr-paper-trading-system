"""OrderManager — translates strategy Signals into broker order requests.

Responsible for:

    Signal → BaseBroker.place_order()

OrderManager does NOT contain strategy logic, risk management,
or position accounting.  It is a thin translation layer between
the strategy output and the broker abstraction.
"""

import logging

from app.broker.base_broker import BaseBroker
from app.models.order import Order, OrderSide
from app.models.signal import Signal, SignalType

logger = logging.getLogger(__name__)


class OrderManager:
    """Translate strategy signals into broker order requests.

    Args:
        broker: A ``BaseBroker`` implementation (injected).
        symbol: The trading instrument symbol.
        quantity: Number of units per order.
        order_type: Order type string (e.g. ``"MARKET"``, ``"LIMIT"``).
    """

    def __init__(
        self,
        broker: BaseBroker,
        symbol: str,
        quantity: int,
        order_type: str = "MARKET",
    ) -> None:
        self._broker = broker
        self._symbol = symbol
        self._quantity = quantity
        self._order_type = order_type

    async def process_signal(self, signal: Signal) -> Order | None:
        """Process a trading signal and submit an order if appropriate.

        Args:
            signal: A ``Signal`` produced by a strategy.

        Returns:
            The ``Order`` returned by the broker for BUY/SELL signals,
            or ``None`` for HOLD.

        Raises:
            Any exception raised by ``broker.place_order()`` is
            propagated after logging context.
        """
        if signal.signal_type == SignalType.HOLD:
            logger.info("HOLD signal received — no order submitted")
            return None

        side = self._resolve_side(signal.signal_type)

        logger.info(
            "%s signal received — submitting %s order: symbol=%s qty=%d type=%s",
            signal.signal_type.value,
            side.value,
            self._symbol,
            self._quantity,
            self._order_type,
        )

        try:
            order = await self._broker.place_order(
                symbol=self._symbol,
                side=side,
                quantity=self._quantity,
                order_type=self._order_type,
            )
        except Exception:
            logger.exception(
                "Broker order submission failed: side=%s symbol=%s qty=%d",
                side.value,
                self._symbol,
                self._quantity,
            )
            raise

        logger.info(
            "%s order submitted: order_id=%s status=%s",
            side.value,
            order.order_id,
            order.status.value,
        )
        return order

    @staticmethod
    def _resolve_side(signal_type: SignalType) -> OrderSide:
        """Map a non-HOLD SignalType to an OrderSide."""
        if signal_type == SignalType.BUY:
            return OrderSide.BUY
        if signal_type == SignalType.SELL:
            return OrderSide.SELL
        msg = f"Unexpected signal type: {signal_type}"
        raise ValueError(msg)
