"""Trading orchestration service connecting market data to execution."""

import logging

from app.market_data.candle_builder import CandleBuilder
from app.models.market_data import MarketDataEvent
from app.models.order import Order
from app.models.signal import Signal
from app.services.order_manager import OrderManager
from app.strategy.base_strategy import BaseStrategy

logger = logging.getLogger(__name__)


class TradingService:
    """Orchestrates the end-to-end trading flow.

    Coordinates the pipeline:
    MarketDataEvent -> CandleBuilder -> completed Candle -> Strategy -> Signal -> OrderManager -> Order.
    """

    def __init__(
        self,
        candle_builder: CandleBuilder,
        strategy: BaseStrategy,
        order_manager: OrderManager,
    ) -> None:
        self._candle_builder = candle_builder
        self._strategy = strategy
        self._order_manager = order_manager

        from app.strategy.candle_strategy_processor import CandleStrategyProcessor

        self._strategy_processor = CandleStrategyProcessor(strategy=strategy)

    async def process_market_data(
        self,
        event: MarketDataEvent,
    ) -> tuple[Signal, Order | None] | None:
        """Process a market-data event.

        Args:
            event: The incoming market-data event.

        Returns:
            A tuple of (Signal, Order | None) if a candle is completed,
            otherwise None.
        """
        logger.debug("Processing market data event: %s", event)

        try:
            completed_candle = self._candle_builder.add_event(event)
        except Exception:
            logger.exception("Error adding event to CandleBuilder")
            raise

        if completed_candle is None:
            return None

        logger.info(
            "Candle completed: timestamp=%s open=%s high=%s low=%s close=%s volume=%d",
            completed_candle.timestamp,
            completed_candle.open,
            completed_candle.high,
            completed_candle.low,
            completed_candle.close,
            completed_candle.volume,
        )

        try:
            signal = self._strategy_processor.process_candle(completed_candle)
        except Exception:
            logger.exception("Error evaluating strategy")
            raise

        try:
            order = await self._order_manager.process_signal(signal)
        except Exception:
            logger.exception(
                "Error processing signal in OrderManager: signal_type=%s reason=%s",
                signal.signal_type.value,
                signal.reason,
            )
            raise

        return signal, order
