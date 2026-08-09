"""Tests for SimulationRunner — Phase 2.8."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock

import pytest

from app.broker.mock_broker import MockBroker
from app.market_data.candle_builder import CandleBuilder
from app.market_data.simulator import MarketDataSimulator
from app.services.order_manager import OrderManager
from app.services.simulation_runner import SimulationRunner
from app.services.trading_service import TradingService
from app.strategy.five_candle_strategy import FiveCandleStrategy


def _create_e2e_runner(
    patterns: list[str],
) -> tuple[SimulationRunner, MockBroker]:
    """Test helper to construct fully isolated E2E components."""
    broker = MockBroker()
    candle_builder = CandleBuilder(timeframe_minutes=5)
    strategy = FiveCandleStrategy()
    order_manager = OrderManager(
        broker=broker,
        symbol="RELIANCE",
        quantity=10,
        order_type="MARKET",
    )
    trading_service = TradingService(
        candle_builder=candle_builder,
        strategy=strategy,
        order_manager=order_manager,
    )
    simulator = MarketDataSimulator(
        symbol="RELIANCE",
        starting_price=Decimal("100.00"),
        patterns=patterns,
        start_time=datetime(2025, 6, 15, 10, 0, tzinfo=UTC),
        ticks_per_candle=5,
        tick_interval_seconds=60,
        volume=100,
    )
    runner = SimulationRunner(
        simulator=simulator,
        trading_service=trading_service,
        broker=broker,
    )
    return runner, broker


class TestSimulationRunner:
    @pytest.mark.asyncio
    async def test_five_bullish_candles_produces_buy(self) -> None:
        """5 completed bullish candles trigger BUY signal, order and long position."""
        runner, broker = _create_e2e_runner(["BULLISH"] * 5)
        await broker.login()

        try:
            result = await runner.run()

            # Verify simulation metrics
            assert result.events_processed == 5 * 5 + 1
            assert result.candles_completed == 5
            assert result.buy_signals == 1
            assert result.sell_signals == 0
            assert result.hold_signals == 4
            assert len(result.orders) == 1

            # Verify order state
            order = result.orders[0]
            assert order.side.name == "BUY"
            assert order.status.name == "FILLED"
            assert order.quantity == 10

            # Verify position via public APIs
            positions = await broker.get_positions()
            assert len(positions) == 1
            pos = positions[0]
            assert pos.symbol == "RELIANCE"
            assert pos.quantity == 10
            assert pos.average_price > 0
        finally:
            await broker.disconnect()

    @pytest.mark.asyncio
    async def test_five_bearish_candles_produces_sell(self) -> None:
        """5 completed bearish candles trigger SELL signal, order and short position."""
        runner, broker = _create_e2e_runner(["BEARISH"] * 5)
        await broker.login()

        try:
            result = await runner.run()

            assert result.events_processed == 5 * 5 + 1
            assert result.candles_completed == 5
            assert result.buy_signals == 0
            assert result.sell_signals == 1
            assert result.hold_signals == 4
            assert len(result.orders) == 1

            order = result.orders[0]
            assert order.side.name == "SELL"
            assert order.status.name == "FILLED"
            assert order.quantity == 10

            positions = await broker.get_positions()
            assert len(positions) == 1
            pos = positions[0]
            assert pos.symbol == "RELIANCE"
            assert pos.quantity == -10
            assert pos.average_price > 0
        finally:
            await broker.disconnect()

    @pytest.mark.asyncio
    async def test_mixed_candles_produces_hold(self) -> None:
        """Mixed bullish/bearish candle sequence produces HOLD and no position."""
        runner, broker = _create_e2e_runner(
            ["BULLISH", "BEARISH", "BULLISH", "BEARISH", "BULLISH"]
        )
        await broker.login()

        try:
            result = await runner.run()

            assert result.events_processed == 5 * 5 + 1
            assert result.candles_completed == 5
            assert result.buy_signals == 0
            assert result.sell_signals == 0
            assert result.hold_signals == 5
            assert len(result.orders) == 0

            positions = await broker.get_positions()
            assert len(positions) == 0  # Flat
        finally:
            await broker.disconnect()

    @pytest.mark.asyncio
    async def test_neutral_candle_produces_hold(self) -> None:
        """A neutral candle in the last 5 completed candles yields HOLD."""
        runner, broker = _create_e2e_runner(
            ["BULLISH", "BULLISH", "BULLISH", "BULLISH", "NEUTRAL"]
        )
        await broker.login()

        try:
            result = await runner.run()

            assert result.candles_completed == 5
            assert result.buy_signals == 0
            assert result.sell_signals == 0
            assert result.hold_signals == 5
            assert len(result.orders) == 0

            positions = await broker.get_positions()
            assert len(positions) == 0
        finally:
            await broker.disconnect()

    @pytest.mark.asyncio
    async def test_insufficient_candles(self) -> None:
        """Generating fewer than 5 completed candles produces no order."""
        runner, broker = _create_e2e_runner(["BULLISH"] * 4)
        await broker.login()

        try:
            result = await runner.run()

            assert result.candles_completed == 4
            assert result.buy_signals == 0
            assert result.sell_signals == 0
            assert result.hold_signals == 4
            assert len(result.orders) == 0

            positions = await broker.get_positions()
            assert len(positions) == 0
        finally:
            await broker.disconnect()

    @pytest.mark.asyncio
    async def test_multiple_signals_and_position_reduction(self) -> None:
        """Longer sequence of BUY then SELL signals reduces position back to flat."""
        runner, broker = _create_e2e_runner((["BULLISH"] * 5) + (["BEARISH"] * 5))
        await broker.login()

        try:
            result = await runner.run()

            assert result.candles_completed == 10
            assert result.buy_signals == 1
            assert result.sell_signals == 1
            assert result.hold_signals == 8
            assert len(result.orders) == 2

            assert result.orders[0].side.name == "BUY"
            assert result.orders[1].side.name == "SELL"

            positions = await broker.get_positions()
            assert len(positions) == 0  # Position is reduced to flat
        finally:
            await broker.disconnect()

    @pytest.mark.asyncio
    async def test_determinism(self) -> None:
        """Running the simulation twice with same config produces identical E2E results."""
        runner1, broker1 = _create_e2e_runner(["BULLISH"] * 5)
        runner2, broker2 = _create_e2e_runner(["BULLISH"] * 5)

        await broker1.login()
        await broker2.login()

        try:
            result1 = await runner1.run()
            result2 = await runner2.run()

            assert result1.events_processed == result2.events_processed
            assert result1.buy_signals == result2.buy_signals

            # Compare orders without order_id and timestamp
            assert len(result1.orders) == len(result2.orders)
            for o1, o2 in zip(result1.orders, result2.orders, strict=True):
                assert o1.symbol == o2.symbol
                assert o1.side == o2.side
                assert o1.quantity == o2.quantity
                assert o1.order_type == o2.order_type
                assert o1.status == o2.status
                assert o1.filled_quantity == o2.filled_quantity
                assert o1.average_fill_price == o2.average_fill_price

            assert await broker1.get_positions() == await broker2.get_positions()
        finally:
            await broker1.disconnect()
            await broker2.disconnect()

    @pytest.mark.asyncio
    async def test_no_duplicate_strategy_evaluation(self) -> None:
        """Verify the runner does not call strategy.evaluate() directly."""
        broker = MockBroker()
        candle_builder = CandleBuilder(timeframe_minutes=5)
        strategy = FiveCandleStrategy()

        order_manager = OrderManager(
            broker=broker,
            symbol="RELIANCE",
            quantity=10,
        )
        trading_service = TradingService(
            candle_builder=candle_builder,
            strategy=strategy,
            order_manager=order_manager,
        )
        simulator = MarketDataSimulator(
            symbol="RELIANCE",
            starting_price=Decimal("100.00"),
            patterns=["BULLISH"] * 5,
            start_time=datetime(2025, 6, 15, 10, 0, tzinfo=UTC),
        )
        runner = SimulationRunner(
            simulator=simulator,
            trading_service=trading_service,
            broker=broker,
        )

        await broker.login()
        try:
            with mock.patch.object(
                strategy, "evaluate", wraps=strategy.evaluate
            ) as spy_evaluate:
                await runner.run()
                # Strategy evaluate should be called exactly 5 times (once per completed candle)
                assert spy_evaluate.call_count == 5
        finally:
            await broker.disconnect()

    @pytest.mark.asyncio
    async def test_error_handling_propagation(self) -> None:
        """Verify exceptions raised during event loop propagate immediately."""
        runner, broker = _create_e2e_runner(["BULLISH"])

        # Mock trading service to raise exception
        with mock.patch.object(
            runner._trading_service,
            "process_market_data",
            side_effect=RuntimeError("Trading service error"),
        ):
            await broker.login()
            try:
                with pytest.raises(RuntimeError, match="Trading service error"):
                    await runner.run()
            finally:
                await broker.disconnect()
