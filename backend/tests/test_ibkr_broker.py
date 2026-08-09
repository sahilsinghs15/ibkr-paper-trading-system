"""Unit tests for the IBKRBroker class."""

import asyncio
from decimal import Decimal
from unittest import mock

import pytest

from app.broker.ibkr.ibkr_broker import IBKRBroker
from app.broker.ibkr.tws_client import TWSClient
from app.core.config import Settings
from app.models.broker import Margin
from app.models.order import OrderSide, OrderStatus


# Helper class to mock Contract in position callbacks
class MockContract:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol


@pytest.fixture
def mock_client() -> mock.Mock:
    """Fixture returning a mocked TWSClient that properly tracks request types."""
    client = mock.Mock(spec=TWSClient)
    client.is_connected.return_value = True

    # Maintain real registry so on_error / namespace isolation works correctly
    _registry: dict[int, str] = {}

    def _register(req_id: int, req_type: str) -> None:
        _registry[req_id] = req_type

    def _unregister(req_id: int) -> None:
        _registry.pop(req_id, None)

    def _get_type(req_id: int) -> str | None:
        return _registry.get(req_id)

    client.register_request_id.side_effect = _register
    client.unregister_request_id.side_effect = _unregister
    client.get_request_type.side_effect = _get_type

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

    async def test_place_order_market_buy(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify placing a market BUY order constructs TWS order, maps fields, and posts request."""
        mock_client.next_order_id = 5000

        order = await broker.place_order(
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=100,
            order_type="MARKET",
        )

        assert order.order_id == "5000"
        assert order.symbol == "AAPL"
        assert order.side == OrderSide.BUY
        assert order.quantity == 100
        assert order.order_type == "MARKET"
        assert order.status == OrderStatus.PENDING

        assert mock_client.next_order_id == 5001

        mock_client.placeOrder.assert_called_once()
        args = mock_client.placeOrder.call_args[0]
        assert args[0] == 5000
        contract = args[1]
        assert contract.symbol == "AAPL"
        ib_order = args[2]
        assert ib_order.action == "BUY"
        assert ib_order.totalQuantity == 100.0
        assert ib_order.orderType == "MKT"
        assert ib_order.transmit is True

    async def test_place_order_limit_sell(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify placing a limit SELL order constructs TWS order, maps price, and posts request."""
        mock_client.next_order_id = 6000

        order = await broker.place_order(
            symbol="MSFT",
            side=OrderSide.SELL,
            quantity=50,
            order_type="LIMIT",
            price=Decimal("250.50"),
        )

        assert order.order_id == "6000"
        assert order.symbol == "MSFT"
        assert order.side == OrderSide.SELL
        assert order.quantity == 50
        assert order.order_type == "LIMIT"
        assert order.price == Decimal("250.50")

        mock_client.placeOrder.assert_called_once()
        args = mock_client.placeOrder.call_args[0]
        assert args[0] == 6000
        ib_order = args[2]
        assert ib_order.action == "SELL"
        assert ib_order.totalQuantity == 50.0
        assert ib_order.orderType == "LMT"
        assert ib_order.lmtPrice == 250.50

    async def test_place_order_argument_validations(self, broker: IBKRBroker) -> None:
        """Verify invalid place_order arguments raise appropriate errors."""
        # Empty symbol
        with pytest.raises(ValueError, match="Symbol must be non-empty"):
            await broker.place_order("", OrderSide.BUY, 10, "MARKET")

        # Zero quantity
        with pytest.raises(ValueError, match="Quantity must be positive"):
            await broker.place_order("AAPL", OrderSide.BUY, 0, "MARKET")

        # Negative quantity
        with pytest.raises(ValueError, match="Quantity must be positive"):
            await broker.place_order("AAPL", OrderSide.BUY, -5, "MARKET")

        # Unsupported order type
        with pytest.raises(ValueError, match="Unsupported order type"):
            await broker.place_order("AAPL", OrderSide.BUY, 10, "STOP")

        # Missing limit price
        with pytest.raises(ValueError, match="Price is required for LIMIT orders"):
            await broker.place_order("AAPL", OrderSide.BUY, 10, "LIMIT")

        # Negative limit price
        with pytest.raises(ValueError, match="Limit price must be positive"):
            await broker.place_order(
                "AAPL", OrderSide.BUY, 10, "LIMIT", Decimal("-10.0")
            )

    async def test_place_order_no_valid_id(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify placing order raises error if next_order_id is not set by handshake."""
        mock_client.next_order_id = None

        with pytest.raises(RuntimeError, match="No nextValidId received from TWS yet"):
            await broker.place_order("AAPL", OrderSide.BUY, 10, "MARKET")

    async def test_order_callbacks_open_order(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify openOrder TWS callbacks correctly update order status in tracking."""
        mock_client.next_order_id = 1000
        await broker.place_order("AAPL", OrderSide.BUY, 10, "MARKET")

        # Simulate callback for Submitted status
        mock_ib_order = mock.Mock()
        mock_ib_order.totalQuantity = 10.0
        mock_ib_order.orderType = "MKT"

        mock_state = mock.Mock()
        mock_state.status = "Submitted"
        broker.on_open_order(1000, mock.Mock(), mock_ib_order, mock_state)

        book = await broker.get_order_book()
        assert book[0].status == OrderStatus.SUBMITTED

        # Simulate callback for PreSubmitted status
        mock_state.status = "PreSubmitted"
        broker.on_open_order(1000, mock.Mock(), mock_ib_order, mock_state)
        book = await broker.get_order_book()
        assert book[0].status == OrderStatus.PENDING

    async def test_order_callbacks_order_status(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify orderStatus callbacks update status, filled quantity, and avg price."""
        mock_client.next_order_id = 2000
        await broker.place_order("AAPL", OrderSide.BUY, 10, "MARKET")

        # 1. Partial fill update
        broker.on_order_status(
            2000, "Submitted", 4.0, 6.0, 150.50, 0, 0, 150.50, 0, "", 0.0
        )
        book = await broker.get_order_book()
        assert book[0].status == OrderStatus.PARTIALLY_FILLED
        assert book[0].filled_quantity == 4
        assert book[0].average_fill_price == Decimal("150.50")

        # 2. Complete fill update
        broker.on_order_status(
            2000, "Filled", 10.0, 0.0, 151.00, 0, 0, 151.00, 0, "", 0.0
        )
        book = await broker.get_order_book()
        assert book[0].status == OrderStatus.FILLED
        assert book[0].filled_quantity == 10
        assert book[0].average_fill_price == Decimal("151.00")

    async def test_order_callbacks_unexpected_id_ignored(
        self, broker: IBKRBroker
    ) -> None:
        """Verify callback with unknown order ID is ignored safely without crashing."""
        # This should not raise or mutate any internal state
        broker.on_order_status(
            9999, "Filled", 10.0, 0.0, 100.0, 0, 0, 100.0, 0, "", 0.0
        )
        book = await broker.get_order_book()
        assert len(book) == 0

    async def test_modify_order_success(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify modify_order resubmits to TWS with correct parameters but does NOT
        prematurely change local state (state is updated only when TWS confirms via openOrder)."""
        mock_client.next_order_id = 3000
        await broker.place_order("AAPL", OrderSide.BUY, 10, "LIMIT", Decimal("150.00"))

        # Modify quantity and price
        modified = await broker.modify_order(
            "3000", quantity=15, price=Decimal("152.50")
        )

        # Returned snapshot reflects original state (not prematurely modified)
        # TWS confirmation via on_open_order() will update local state.
        assert modified.quantity == 10
        assert modified.price == Decimal("150.00")

        # Verify placeOrder was called twice (once for place, once for modify) with same order ID
        assert mock_client.placeOrder.call_count == 2
        args = mock_client.placeOrder.call_args_list[1][0]
        assert args[0] == 3000
        ib_order = args[2]
        assert ib_order.totalQuantity == 15.0
        assert ib_order.lmtPrice == 152.50

    async def test_modify_order_invalid_inputs(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify modify_order validation logic for missing, terminal, or malformed modifications."""
        mock_client.next_order_id = 4000
        await broker.place_order("AAPL", OrderSide.BUY, 10, "LIMIT", Decimal("150.00"))

        # 1. Unknown order ID
        with pytest.raises(ValueError, match="Order not found"):
            await broker.modify_order("9999", quantity=15)

        # 2. Invalid quantity
        with pytest.raises(ValueError, match="Quantity must be positive"):
            await broker.modify_order("4000", quantity=0)

        # 3. Invalid price
        with pytest.raises(ValueError, match="Limit price must be positive"):
            await broker.modify_order("4000", price=Decimal("-5.00"))

        # 4. MARKET order price modification
        mock_client.next_order_id = 4001
        await broker.place_order("AAPL", OrderSide.BUY, 10, "MARKET")
        with pytest.raises(ValueError, match="Cannot set price for a MARKET order"):
            await broker.modify_order("4001", price=Decimal("100.00"))

        # 5. Terminal order modification
        # Complete fill
        broker.on_order_status(
            4000, "Filled", 10.0, 0.0, 150.00, 0, 0, 150.00, 0, "", 0.0
        )
        with pytest.raises(ValueError, match="Cannot modify order in terminal state"):
            await broker.modify_order("4000", quantity=20)

    async def test_cancel_order_success(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify cancel_order posts cancel to TWS and does not change status until callback."""
        mock_client.next_order_id = 5000
        await broker.place_order("AAPL", OrderSide.BUY, 10, "MARKET")

        cancelled = await broker.cancel_order("5000")
        # Status should remain PENDING until callback confirms it
        assert cancelled.status == OrderStatus.PENDING

        mock_client.cancelOrder.assert_called_once_with(5000)

        # Simulate cancellation callback
        broker.on_order_status(5000, "Cancelled", 0.0, 10.0, 0.0, 0, 0, 0.0, 0, "", 0.0)
        book = await broker.get_order_book()
        assert book[0].status == OrderStatus.CANCELLED

    async def test_cancel_order_invalid(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify cancel_order rejects unknown or terminal orders."""
        mock_client.next_order_id = 6000
        await broker.place_order("AAPL", OrderSide.BUY, 10, "MARKET")

        # Unknown order
        with pytest.raises(ValueError, match="Order not found"):
            await broker.cancel_order("9999")

        # Terminal order
        broker.on_order_status(6000, "Cancelled", 0.0, 10.0, 0.0, 0, 0, 0.0, 0, "", 0.0)
        with pytest.raises(ValueError, match="Cannot cancel order in terminal state"):
            await broker.cancel_order("6000")

    async def test_order_error_callbacks(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify that TWS error callbacks transition order statuses correctly."""
        mock_client.next_order_id = 7000
        await broker.place_order("AAPL", OrderSide.BUY, 10, "MARKET")

        # Error code 202 is cancellation — mock_client registry has 7000 as "order"
        broker.on_error(7000, 202, "Order cancelled by user")
        book = await broker.get_order_book()
        assert book[0].status == OrderStatus.CANCELLED

        # Other error code is rejection
        mock_client.next_order_id = 7001
        await broker.place_order("AAPL", OrderSide.BUY, 10, "MARKET")
        # 7001 is registered as "order" by place_order
        broker.on_error(7001, 201, "Order rejected: insufficient funds")
        book = await broker.get_order_book()
        order_7001 = next(o for o in book if o.order_id == "7001")
        assert order_7001.status == OrderStatus.REJECTED

    async def test_multiple_orders_isolation(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify interleaved callbacks for overlapping orders update only their targets."""
        mock_client.next_order_id = 8000
        await broker.place_order("AAPL", OrderSide.BUY, 100, "MARKET")
        await broker.place_order("GOOG", OrderSide.SELL, 50, "LIMIT", Decimal("150.00"))

        # Interleave status updates
        broker.on_order_status(
            8000, "Submitted", 0.0, 100.0, 0.0, 0, 0, 0.0, 0, "", 0.0
        )
        broker.on_order_status(8001, "Submitted", 0.0, 50.0, 0.0, 0, 0, 0.0, 0, "", 0.0)

        # Update order A
        broker.on_order_status(
            8000, "Filled", 100.0, 0.0, 150.50, 0, 0, 150.50, 0, "", 0.0
        )

        book = await broker.get_order_book()
        order_a = next(o for o in book if o.order_id == "8000")
        order_b = next(o for o in book if o.order_id == "8001")

        assert order_a.status == OrderStatus.FILLED
        assert order_a.filled_quantity == 100
        assert order_b.status == OrderStatus.SUBMITTED
        assert order_b.filled_quantity == 0

    async def test_disconnect_keeps_order_state(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify TWS disconnect does not alter open order status to false FILLED/CANCELLED."""
        mock_client.next_order_id = 9000
        await broker.place_order("AAPL", OrderSide.BUY, 10, "MARKET")

        broker.on_order_status(9000, "Submitted", 0.0, 10.0, 0.0, 0, 0, 0.0, 0, "", 0.0)

        # Disconnect
        broker.on_connection_closed()

        book = await broker.get_order_book()
        assert book[0].status == OrderStatus.SUBMITTED

    async def test_terminal_filled_cannot_regress_to_submitted(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify that an order in terminal FILLED status cannot regress back to SUBMITTED."""
        mock_client.next_order_id = 9500
        await broker.place_order("AAPL", OrderSide.BUY, 10, "MARKET")

        # Transition to FILLED
        broker.on_order_status(
            9500, "Filled", 10.0, 0.0, 150.00, 0, 0, 150.00, 0, "", 0.0
        )
        book = await broker.get_order_book()
        assert book[0].status == OrderStatus.FILLED

        # Late Submitted callback should be ignored for status mutation
        broker.on_order_status(9500, "Submitted", 0.0, 10.0, 0.0, 0, 0, 0.0, 0, "", 0.0)
        book = await broker.get_order_book()
        assert book[0].status == OrderStatus.FILLED

    async def test_terminal_cancelled_cannot_regress_to_submitted(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify that an order in terminal CANCELLED status cannot regress back to SUBMITTED."""
        mock_client.next_order_id = 9501
        await broker.place_order("AAPL", OrderSide.BUY, 10, "MARKET")

        # Transition to CANCELLED
        broker.on_order_status(9501, "Cancelled", 0.0, 10.0, 0.0, 0, 0, 0.0, 0, "", 0.0)
        book = await broker.get_order_book()
        assert book[0].status == OrderStatus.CANCELLED

        # Late Submitted callback should be ignored for status mutation
        broker.on_order_status(9501, "Submitted", 0.0, 10.0, 0.0, 0, 0, 0.0, 0, "", 0.0)
        book = await broker.get_order_book()
        assert book[0].status == OrderStatus.CANCELLED

    async def test_terminal_rejected_cannot_regress_to_submitted(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify that an order in terminal REJECTED status cannot regress back to SUBMITTED."""
        mock_client.next_order_id = 9502
        await broker.place_order("AAPL", OrderSide.BUY, 10, "MARKET")

        # Transition to REJECTED
        broker.on_order_status(9502, "Inactive", 0.0, 10.0, 0.0, 0, 0, 0.0, 0, "", 0.0)
        book = await broker.get_order_book()
        assert book[0].status == OrderStatus.REJECTED

        # Late Submitted callback should be ignored for status mutation
        broker.on_order_status(9502, "Submitted", 0.0, 10.0, 0.0, 0, 0, 0.0, 0, "", 0.0)
        book = await broker.get_order_book()
        assert book[0].status == OrderStatus.REJECTED

    async def test_unrelated_request_error_does_not_mutate_order(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify that an error on an unrelated request ID does not mutate an order with the same ID."""
        mock_client.next_order_id = 10001
        await broker.place_order("AAPL", OrderSide.BUY, 10, "MARKET")

        # Unrelated request type error with same ID 10001 (e.g. registered as "market_data")
        broker._client.register_request_id(10001, "market_data")

        # Trigger TWS error on 10001
        broker.on_error(10001, 201, "Unrelated error message")

        # Verify order is still in PENDING (not mutated to REJECTED)
        book = await broker.get_order_book()
        assert book[0].status == OrderStatus.PENDING

    async def test_correct_order_error_still_mutates_order(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify that an error on a registered order ID successfully rejects/cancels the order."""
        mock_client.next_order_id = 10002
        await broker.place_order("AAPL", OrderSide.BUY, 10, "MARKET")

        # Trigger TWS error on 10002
        broker.on_error(10002, 201, "Order rejected by TWS")

        # Order should be REJECTED
        book = await broker.get_order_book()
        assert book[0].status == OrderStatus.REJECTED

    async def test_pending_cancel_is_non_terminal(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify that PendingCancel IBKR status maps to non-terminal SUBMITTED state."""
        mock_client.next_order_id = 10003
        await broker.place_order("AAPL", OrderSide.BUY, 10, "MARKET")

        # Simulate PendingCancel status callback
        broker.on_order_status(
            10003, "PendingCancel", 0.0, 10.0, 0.0, 0, 0, 0.0, 0, "", 0.0
        )
        book = await broker.get_order_book()
        assert book[0].status == OrderStatus.SUBMITTED

    async def test_cancelled_terminal_only_after_actual_confirmation(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify that cancellation only transitions to terminal CANCELLED after actual confirmation."""
        mock_client.next_order_id = 10004
        await broker.place_order("AAPL", OrderSide.BUY, 10, "MARKET")

        # 1. PendingCancel (non-terminal)
        broker.on_order_status(
            10004, "PendingCancel", 0.0, 10.0, 0.0, 0, 0, 0.0, 0, "", 0.0
        )
        book = await broker.get_order_book()
        assert book[0].status == OrderStatus.SUBMITTED

        # 2. Confirmed Cancelled (terminal)
        broker.on_order_status(
            10004, "Cancelled", 0.0, 10.0, 0.0, 0, 0, 0.0, 0, "", 0.0
        )
        book = await broker.get_order_book()
        assert book[0].status == OrderStatus.CANCELLED

    async def test_modify_request_does_not_prematurely_change_local_state(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify that calling modify_order does not immediately change price/quantity locally."""
        mock_client.next_order_id = 10005
        await broker.place_order("AAPL", OrderSide.BUY, 10, "LIMIT", Decimal("150.00"))

        # Modify quantity and price
        await broker.modify_order("10005", quantity=15, price=Decimal("152.00"))

        # Local state should still reflect original price/quantity until confirmed
        book = await broker.get_order_book()
        assert book[0].quantity == 10
        assert book[0].price == Decimal("150.00")

    async def test_successful_modify_tws_confirmation_updates_quantity_price(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify that openOrder callback from TWS updates the locally modified quantity and price."""
        mock_client.next_order_id = 10006
        await broker.place_order("AAPL", OrderSide.BUY, 10, "LIMIT", Decimal("150.00"))

        await broker.modify_order("10006", quantity=15, price=Decimal("152.00"))

        # Simulate openOrder callback from TWS with modified parameters
        mock_ib_order = mock.Mock()
        mock_ib_order.totalQuantity = 15.0
        mock_ib_order.orderType = "LMT"
        mock_ib_order.lmtPrice = 152.00
        mock_state = mock.Mock()
        mock_state.status = "Submitted"

        broker.on_open_order(10006, mock.Mock(), mock_ib_order, mock_state)

        # Verify local state is updated
        book = await broker.get_order_book()
        assert book[0].quantity == 15
        assert book[0].price == Decimal("152.00")
        assert book[0].status == OrderStatus.SUBMITTED

    async def test_failed_modification_preserves_actual_existing_state(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify that if modify_order fails or is rejected, the original state is preserved."""
        mock_client.next_order_id = 10007
        await broker.place_order("AAPL", OrderSide.BUY, 10, "LIMIT", Decimal("150.00"))

        await broker.modify_order("10007", quantity=15, price=Decimal("152.00"))

        # Simulate error on modification
        broker.on_error(10007, 201, "Modification rejected")

        # Status becomes REJECTED, but price and quantity must remain original!
        book = await broker.get_order_book()
        assert book[0].status == OrderStatus.REJECTED
        assert book[0].quantity == 10
        assert book[0].price == Decimal("150.00")

    async def test_place_order_exception_does_not_leave_phantom_order(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify that if placeOrder raises exception, the domain order is removed from book."""
        mock_client.next_order_id = 10008
        mock_client.placeOrder.side_effect = RuntimeError("Socket write failed")

        with pytest.raises(RuntimeError, match="Socket write failed"):
            await broker.place_order("AAPL", OrderSide.BUY, 10, "MARKET")

        book = await broker.get_order_book()
        assert len(book) == 0

    async def test_terminal_filled_retains_fill_info_after_stale_callback(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify a stale callback after terminal FILLED does not regress recorded fill info."""
        mock_client.next_order_id = 10009
        await broker.place_order("AAPL", OrderSide.BUY, 10, "MARKET")

        # Full fill with recorded execution data
        broker.on_order_status(
            10009, "Filled", 10.0, 0.0, 150.00, 0, 0, 150.00, 0, "", 0.0
        )
        book = await broker.get_order_book()
        assert book[0].status == OrderStatus.FILLED
        assert book[0].filled_quantity == 10
        assert book[0].average_fill_price == Decimal("150.00")

        # Late/out-of-order callback with stale execution data must not regress anything
        broker.on_order_status(
            10009, "Submitted", 0.0, 10.0, 0.0, 0, 0, 0.0, 0, "", 0.0
        )
        book = await broker.get_order_book()
        assert book[0].status == OrderStatus.FILLED
        assert book[0].filled_quantity == 10
        assert book[0].average_fill_price == Decimal("150.00")

    async def test_out_of_order_callback_does_not_regress_fill_info(
        self, broker: IBKRBroker, mock_client: mock.Mock
    ) -> None:
        """Verify a stale out-of-order partial-fill callback does not regress filled quantity."""
        mock_client.next_order_id = 10010
        await broker.place_order("AAPL", OrderSide.BUY, 10, "MARKET")

        # Partial fill is recorded
        broker.on_order_status(
            10010, "Submitted", 4.0, 6.0, 149.50, 0, 0, 149.50, 0, "", 0.0
        )
        book = await broker.get_order_book()
        assert book[0].status == OrderStatus.PARTIALLY_FILLED
        assert book[0].filled_quantity == 4
        assert book[0].average_fill_price == Decimal("149.50")

        # Stale callback (filled=0) arrives after the partial fill and must not erase it
        broker.on_order_status(
            10010, "Submitted", 0.0, 10.0, 0.0, 0, 0, 0.0, 0, "", 0.0
        )
        book = await broker.get_order_book()
        assert book[0].filled_quantity == 4
        assert book[0].average_fill_price == Decimal("149.50")

        # A later genuine fill still advances fill info from the recorded baseline
        broker.on_order_status(
            10010, "Filled", 10.0, 0.0, 151.00, 0, 0, 151.00, 0, "", 0.0
        )
        book = await broker.get_order_book()
        assert book[0].status == OrderStatus.FILLED
        assert book[0].filled_quantity == 10
        assert book[0].average_fill_price == Decimal("151.00")
