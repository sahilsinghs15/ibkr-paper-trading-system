"""Unit tests for the IBKRBroker class."""

import asyncio
from decimal import Decimal
from unittest import mock

import pytest

from app.broker.ibkr.ibkr_broker import IBKRBroker
from app.broker.ibkr.tws_client import TWSClient
from app.core.config import Settings
from app.models.broker import Margin
from app.models.order import OrderSide


# Helper class to mock Contract in position callbacks
class MockContract:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol


@pytest.fixture
def mock_client() -> mock.Mock:
    """Fixture returning a mocked TWSClient."""
    client = mock.Mock(spec=TWSClient)
    client.is_connected.return_value = True
    return client


@pytest.fixture
def settings() -> Settings:
    """Fixture returning settings with short timeout for fast tests."""
    return Settings(
        ibkr_connection_timeout=1,
    )


@pytest.fixture
def broker(mock_client: mock.Mock, settings: Settings) -> IBKRBroker:
    """Fixture returning an IBKRBroker instance."""
    return IBKRBroker(client=mock_client, settings=settings)


@pytest.mark.asyncio
class TestIBKRBroker:
    """Test suite verifying read-only IBKRBroker operations."""

    async def test_require_connected_raises(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify operations raise ConnectionError when client is disconnected."""
        mock_client.is_connected.return_value = False

        with pytest.raises(ConnectionError, match="Broker is not connected"):
            await broker.get_positions()

        with pytest.raises(ConnectionError, match="Broker is not connected"):
            await broker.get_margin()

    async def test_login_success(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify successful login initiates connection correctly."""
        mock_client.connect_and_start.return_value = True

        await broker.login()

        mock_client.connect_and_start.assert_called_once_with(
            host=broker._settings.ibkr_host,
            port=broker._settings.ibkr_port,
            client_id=broker._settings.ibkr_client_id,
            timeout=float(broker._settings.ibkr_connection_timeout),
        )

    async def test_login_fails(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify connection failure raises ConnectionError."""
        mock_client.connect_and_start.return_value = False

        with pytest.raises(
            ConnectionError, match="Failed to connect and handshake with TWS"
        ):
            await broker.login()

    async def test_disconnect(self, broker: IBKRBroker, mock_client: mock.Mock) -> None:
        """Verify disconnect shuts down the connection cleanly."""
        await broker.disconnect()
        mock_client.disconnect_clean.assert_called_once()

    async def test_get_margin_success(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify successful account summary request parses required tags into Margin."""

        # Trigger the callback in the background
        async def trigger_callbacks() -> None:
            # Wait a tick for request to be registered
            await asyncio.sleep(0.01)
            # Find the req_id registered in the adapter
            req_ids = list(broker._margin_futures.keys())
            assert len(req_ids) == 1
            req_id = req_ids[0]

            broker.on_account_summary(
                req_id, "U12345", "NetLiquidation", "100000.50", "USD"
            )
            broker.on_account_summary(
                req_id, "U12345", "AvailableFunds", "80000.25", "USD"
            )
            broker.on_account_summary(
                req_id, "U12345", "BuyingPower", "200000.00", "USD"
            )
            broker.on_account_summary_end(req_id)

        task = asyncio.create_task(trigger_callbacks())
        margin = await broker.get_margin()
        await task

        assert isinstance(margin, Margin)
        assert margin.equity == Decimal("100000.50")
        assert margin.available_funds == Decimal("80000.25")
        assert margin.buying_power == Decimal("200000.00")

        # Verify state is cleared
        assert len(broker._margin_futures) == 0
        assert len(broker._margin_data) == 0
        # Verify cancelAccountSummary was called upon completion
        mock_client.cancelAccountSummary.assert_called()

    async def test_get_margin_missing_value(self, broker: IBKRBroker) -> None:
        """Verify missing required tag raises ValueError on completion."""

        async def trigger_callbacks() -> None:
            await asyncio.sleep(0.01)
            req_id = next(iter(broker._margin_futures.keys()))
            broker.on_account_summary(
                req_id, "U12345", "NetLiquidation", "100000.50", "USD"
            )
            # Skip AvailableFunds
            broker.on_account_summary(
                req_id, "U12345", "BuyingPower", "200000.00", "USD"
            )
            broker.on_account_summary_end(req_id)

        task = asyncio.create_task(trigger_callbacks())
        with pytest.raises(ValueError, match="Missing required account summary values"):
            await broker.get_margin()
        await task

    async def test_get_margin_timeout(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify Margin request timeout cleans up state and raises TimeoutError."""
        # Set short connection timeout for fast testing
        broker._timeout = 0.05

        with pytest.raises(TimeoutError, match="Margin request timed out"):
            await broker.get_margin()

        # Verify state is cleaned up
        assert len(broker._margin_futures) == 0
        assert len(broker._margin_data) == 0
        mock_client.cancelAccountSummary.assert_called_once()

    async def test_get_margin_tws_error(self, broker: IBKRBroker) -> None:
        """Verify TWS error fails the margin request and propagates the error."""

        async def trigger_error() -> None:
            await asyncio.sleep(0.01)
            req_id = next(iter(broker._margin_futures.keys()))
            broker.on_error(req_id, 200, "No security definition found")

        task = asyncio.create_task(trigger_error())
        with pytest.raises(RuntimeError, match="TWS error reqId=.* code=200"):
            await broker.get_margin()
        await task

        assert len(broker._margin_futures) == 0

    async def test_get_margin_late_callback(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify late callback for completed request does not corrupt active state."""

        # Request 1: Complete normally
        async def trigger_first() -> None:
            await asyncio.sleep(0.01)
            req_id = next(iter(broker._margin_futures.keys()))
            broker.on_account_summary(
                req_id, "U12345", "NetLiquidation", "100.0", "USD"
            )
            broker.on_account_summary(req_id, "U12345", "AvailableFunds", "80.0", "USD")
            broker.on_account_summary(req_id, "U12345", "BuyingPower", "200.0", "USD")
            broker.on_account_summary_end(req_id)

        task1 = asyncio.create_task(trigger_first())
        res1 = await broker.get_margin()
        await task1
        assert res1.equity == Decimal("100.0")

        # Now, send a late callback for the old reqId
        old_req_id = 1000
        broker.on_account_summary(
            old_req_id, "U12345", "NetLiquidation", "999.0", "USD"
        )
        assert len(broker._margin_data) == 0  # Ignored

    async def test_get_margin_disconnect_during_request(
        self, broker: IBKRBroker
    ) -> None:
        """Verify TWS connection drop fails pending margin requests immediately."""

        async def trigger_disconnect() -> None:
            await asyncio.sleep(0.01)
            broker.on_connection_closed()

        task = asyncio.create_task(trigger_disconnect())
        with pytest.raises(
            ConnectionError, match="TWS connection closed during request"
        ):
            await broker.get_margin()
        await task

        assert len(broker._margin_futures) == 0

    async def test_get_positions_success(self, broker: IBKRBroker) -> None:
        """Verify dynamic symbol mapping, float parsing, and flat position filtering."""

        async def trigger_callbacks() -> None:
            await asyncio.sleep(0.01)
            # Long position
            broker.on_position("U12345", MockContract("AAPL"), 100.0, 150.25)
            # Short position
            broker.on_position("U12345", MockContract("GOOG"), -50.0, 100.50)
            # Flat position (should be filtered out)
            broker.on_position("U12345", MockContract("MSFT"), 0.0, 400.00)
            broker.on_position_end()

        task = asyncio.create_task(trigger_callbacks())
        positions = await broker.get_positions()
        await task

        assert len(positions) == 2

        # Verify AAPL (Long)
        p1 = next(p for p in positions if p.symbol == "AAPL")
        assert p1.quantity == 100
        assert p1.average_price == Decimal("150.25")

        # Verify GOOG (Short)
        p2 = next(p for p in positions if p.symbol == "GOOG")
        assert p2.quantity == -50
        assert p2.average_price == Decimal("100.50")

        # Verify state is cleared
        assert broker._positions_future is None
        assert len(broker._collected_positions) == 0

    async def test_get_positions_concurrent_raises(self, broker: IBKRBroker) -> None:
        """Verify concurrent position requests are rejected with RuntimeError."""
        # Initiate first request
        task1 = asyncio.create_task(broker.get_positions())
        await asyncio.sleep(0.01)

        # Try a second request concurrently
        with pytest.raises(
            RuntimeError, match="positions request is already in progress"
        ):
            await broker.get_positions()

        # Clean up the first active future to prevent hang/timeout in test suite
        broker.on_position_end()
        await task1

    async def test_get_positions_timeout(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify positions timeout resets state and calls cancelPositions."""
        broker._timeout = 0.05

        with pytest.raises(TimeoutError, match="Positions request timed out"):
            await broker.get_positions()

        assert broker._positions_future is None
        assert len(broker._collected_positions) == 0
        mock_client.cancelPositions.assert_called_once()

    async def test_get_positions_disconnect_during_request(
        self, broker: IBKRBroker
    ) -> None:
        """Verify TWS disconnect fails pending position requests."""

        async def trigger_disconnect() -> None:
            await asyncio.sleep(0.01)
            broker.on_connection_closed()

        task = asyncio.create_task(trigger_disconnect())
        with pytest.raises(
            ConnectionError, match="TWS connection closed during request"
        ):
            await broker.get_positions()
        await task

        assert broker._positions_future is None
        assert len(broker._collected_positions) == 0

    async def test_get_positions_tws_error(self, broker: IBKRBroker) -> None:
        """Verify general TWS error fails active positions request."""

        async def trigger_error() -> None:
            await asyncio.sleep(0.01)
            broker.on_error(-1, 504, "Connection lost")

        task = asyncio.create_task(trigger_error())
        with pytest.raises(RuntimeError, match="TWS error reqId=-1 code=504"):
            await broker.get_positions()
        await task

        assert broker._positions_future is None

    async def test_unimplemented_order_methods_raise(self, broker: IBKRBroker) -> None:
        """Verify order methods raise NotImplementedError in this phase."""
        with pytest.raises(
            NotImplementedError, match="Order placement is not supported"
        ):
            await broker.place_order("AAPL", OrderSide.BUY, 10, "MARKET")

        with pytest.raises(
            NotImplementedError, match="Order modification is not supported"
        ):
            await broker.modify_order("some_id", 20, Decimal(100))

        with pytest.raises(
            NotImplementedError, match="Order cancellation is not supported"
        ):
            await broker.cancel_order("some_id")

        with pytest.raises(
            NotImplementedError, match="Order book retrieval is not supported"
        ):
            await broker.get_order_book()
