"""Simulation runner orchestrating end-to-end paper trading simulations."""

import logging
from dataclasses import dataclass, field

from app.broker.base_broker import BaseBroker
from app.market_data.simulator import MarketDataSimulator
from app.models.broker import Margin
from app.models.order import Order
from app.models.position import Position
from app.models.signal import Signal, SignalType
from app.services.trading_service import TradingService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SimulationResult:
    """Detailed summary of the end-to-end simulation run.

    Provides the metrics collected from the public boundaries of the components.
    """

    events_processed: int
    candles_completed: int
    buy_signals: int
    sell_signals: int
    hold_signals: int
    signals: list[Signal] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    final_positions: list[Position] = field(default_factory=list)
    final_margin: Margin | None = None


class SimulationRunner:
    """Orchestrates an end-to-end paper-trading simulation.

    Iterates over ticks from the MarketDataSimulator, feeds them to the
    TradingService, and queries the BaseBroker for final state.
    """

    def __init__(
        self,
        simulator: MarketDataSimulator,
        trading_service: TradingService,
        broker: BaseBroker,
    ) -> None:
        """Initialize the SimulationRunner with injected components."""
        self._simulator = simulator
        self._trading_service = trading_service
        self._broker = broker

    async def run(self) -> SimulationResult:
        """Run the E2E simulation synchronously and return metrics."""
        logger.info("Simulation started.")

        events_processed = 0
        candles_completed = 0
        buy_signals = 0
        sell_signals = 0
        hold_signals = 0

        signals: list[Signal] = []
        orders: list[Order] = []

        try:
            for event in self._simulator.iter_events():
                events_processed += 1
                logger.debug("Processing market-data event tick: %s", event)

                result = await self._trading_service.process_market_data(event)

                if result is not None:
                    candles_completed += 1
                    signal, order = result
                    signals.append(signal)

                    if signal.signal_type == SignalType.BUY:
                        buy_signals += 1
                    elif signal.signal_type == SignalType.SELL:
                        sell_signals += 1
                    elif signal.signal_type == SignalType.HOLD:
                        hold_signals += 1

                    if order is not None:
                        # Auto-fill MockBroker orders to update position immediately
                        if hasattr(self._broker, "simulate_fill"):
                            logger.debug(
                                "Simulating instant fill for order: %s",
                                order.order_id,
                            )
                            filled_order = await self._broker.simulate_fill(
                                order.order_id
                            )
                            orders.append(filled_order)
                        else:
                            orders.append(order)

        except Exception:
            logger.exception(
                "Simulation stopped due to exception at event %d",
                events_processed,
            )
            raise

        # Query final account states via BaseBroker abstraction
        final_positions = await self._broker.get_positions()
        final_margin = await self._broker.get_margin()

        logger.info("Simulation completed.")
        logger.info("Total events processed: %d", events_processed)
        logger.info("Completed candles: %d", candles_completed)
        logger.info(
            "Signals generated: BUY=%d, SELL=%d, HOLD=%d",
            buy_signals,
            sell_signals,
            hold_signals,
        )
        logger.info("Orders generated: %d", len(orders))
        logger.info("Final position summary: %s", final_positions)

        return SimulationResult(
            events_processed=events_processed,
            candles_completed=candles_completed,
            buy_signals=buy_signals,
            sell_signals=sell_signals,
            hold_signals=hold_signals,
            signals=signals,
            orders=orders,
            final_positions=final_positions,
            final_margin=final_margin,
        )
