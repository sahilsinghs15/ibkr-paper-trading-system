"""IBKR Broker Adapter implementation."""

import asyncio
import copy
import logging
import threading
from decimal import Decimal
from typing import Any

from ibapi.contract import Contract  # type: ignore[import-untyped]
from ibapi.order import Order as IBOrder  # type: ignore[import-untyped]

from app.broker.base_broker import BaseBroker
from app.broker.ibkr.tws_client import TWSClient
from app.core.config import Settings
from app.models.broker import Margin
from app.models.order import Order, OrderSide, OrderStatus
from app.models.position import Position

logger = logging.getLogger(__name__)


class IBKRBroker(BaseBroker):
    """Adapter bridging TWS Client to BaseBroker interface."""

    def __init__(self, client: TWSClient, settings: Settings) -> None:
        """Initialize the broker adapter with connection credentials and state."""
        self._client = client
        self._settings = settings
        self._timeout = float(settings.ibkr_connection_timeout)

        self._lock = threading.Lock()

        # Counter for reqAccountSummary IDs (using a high base for segregation)
        self._req_id_counter = 10000000

        # Register self as a listener for callbacks from TWSClient
        self._client.register_listener(self)

        # Asynchronous state management for positions
        self._positions_future: asyncio.Future[list[Position]] | None = None
        self._positions_loop: asyncio.AbstractEventLoop | None = None
        self._collected_positions: list[Position] = []

        # Asynchronous state management for margin details (keyed by req_id)
        self._margin_futures: dict[
            int, tuple[asyncio.Future[Margin], asyncio.AbstractEventLoop]
        ] = {}
        self._margin_data: dict[int, dict[str, Decimal]] = {}

        # Dictionary to track placed orders by ID
        self._orders: dict[str, Order] = {}

    def _require_connected(self) -> None:
        """Check connection state and raise ConnectionError if client is disconnected."""
        if not self._client.is_connected():
            raise ConnectionError("Broker is not connected to TWS.")

    # ── BaseBroker interface implementation ─────────────────────────

    async def login(self) -> None:
        """Establish connection with TWS Demo/Gateway."""
        success = self._client.connect_and_start(
            host=self._settings.ibkr_host,
            port=self._settings.ibkr_port,
            client_id=self._settings.ibkr_client_id,
            timeout=float(self._settings.ibkr_connection_timeout),
        )
        if not success:
            raise ConnectionError("Failed to connect and handshake with TWS.")
        logger.info("IBKRBroker login successful.")

    async def disconnect(self) -> None:
        """Cleanly disconnect from TWS."""
        self._client.disconnect_clean()
        logger.info("IBKRBroker disconnect successful.")

    async def get_positions(self) -> list[Position]:
        """Request all active positions from TWS and wait for completion."""
        self._require_connected()

        loop = asyncio.get_running_loop()
        future = loop.create_future()

        with self._lock:
            if self._positions_future is not None:
                raise RuntimeError("A positions request is already in progress.")
            self._positions_future = future
            self._positions_loop = loop
            self._collected_positions = []

        # Request positions outside the lock
        logger.info("Requesting positions from TWS...")
        self._client.reqPositions()

        try:
            return await asyncio.wait_for(future, timeout=self._timeout)
        except TimeoutError:
            logger.error("Positions request timed out.")
            with self._lock:
                self._positions_future = None
                self._positions_loop = None
                self._collected_positions = []
            try:
                self._client.cancelPositions()
            except Exception as e:  # noqa: BLE001
                logger.debug("Failed to cancel positions: %s", e)
            raise TimeoutError("Positions request timed out.")
        except Exception:
            with self._lock:
                self._positions_future = None
                self._positions_loop = None
                self._collected_positions = []
            try:
                self._client.cancelPositions()
            except Exception as e:  # noqa: BLE001
                logger.debug("Failed to cancel positions: %s", e)
            raise

    async def get_margin(self) -> Margin:
        """Request account summary and construct a Margin domain model."""
        self._require_connected()

        loop = asyncio.get_running_loop()
        future = loop.create_future()

        with self._lock:
            req_id = self._req_id_counter
            self._req_id_counter += 1
            self._margin_futures[req_id] = (future, loop)
            self._margin_data[req_id] = {}

        # Register request ID as margin type
        self._client.register_request_id(req_id, "margin")

        # Call reqAccountSummary outside the lock
        tags = "NetLiquidation,AvailableFunds,BuyingPower"
        logger.info("Requesting account summary for reqId=%d...", req_id)
        self._client.reqAccountSummary(req_id, "All", tags)

        try:
            res = await asyncio.wait_for(future, timeout=self._timeout)
            self._client.unregister_request_id(req_id)
            return res
        except TimeoutError:
            logger.error("Margin request timed out for reqId=%d.", req_id)
            with self._lock:
                self._margin_futures.pop(req_id, None)
                self._margin_data.pop(req_id, None)
            self._client.unregister_request_id(req_id)
            try:
                self._client.cancelAccountSummary(req_id)
            except Exception as e:  # noqa: BLE001
                logger.debug("Failed to cancel account summary: %s", e)
            raise TimeoutError("Margin request timed out.")
        except Exception:
            with self._lock:
                self._margin_futures.pop(req_id, None)
                self._margin_data.pop(req_id, None)
            self._client.unregister_request_id(req_id)
            try:
                self._client.cancelAccountSummary(req_id)
            except Exception as e:  # noqa: BLE001
                logger.debug("Failed to cancel account summary: %s", e)
            raise

    def _get_next_order_id(self) -> int:
        """Reserve and return the next valid TWS order ID under lock."""
        with self._lock:
            current_id = self._client.next_order_id
            if current_id is None:
                raise RuntimeError("No nextValidId received from TWS yet.")
            self._client.next_order_id = current_id + 1
            return current_id

    def _map_status(self, ib_status: str) -> OrderStatus:
        """Map IBKR order status string to domain OrderStatus enum."""
        status_upper = ib_status.upper()
        if status_upper in ("PENDINGSUBMIT", "PRESUBMITTED", "APIPENDING"):
            return OrderStatus.PENDING
        elif status_upper in ("SUBMITTED", "PENDINGCANCEL"):
            return OrderStatus.SUBMITTED
        elif status_upper in ("FILLED",):
            return OrderStatus.FILLED
        elif status_upper in ("CANCELLED", "APICANCELLED"):
            return OrderStatus.CANCELLED
        elif status_upper in ("INACTIVE", "REJECTED"):
            return OrderStatus.REJECTED
        else:
            logger.warning(
                "Unknown IBKR order status received: %s. Defaulting to SUBMITTED.",
                ib_status,
            )
            return OrderStatus.SUBMITTED

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: int,
        order_type: str,
        price: Decimal | None = None,
    ) -> Order:
        """Place an order with TWS and track its lifecycle."""
        self._require_connected()

        # Argument validation
        if not symbol or not symbol.strip():
            raise ValueError("Symbol must be non-empty.")
        if quantity <= 0:
            raise ValueError(f"Quantity must be positive, got {quantity}.")

        # Order type and price validation
        order_type_upper = order_type.upper()
        if order_type_upper not in ("MARKET", "LIMIT"):
            raise ValueError(f"Unsupported order type: {order_type}")

        if order_type_upper == "LIMIT":
            if price is None:
                raise ValueError("Price is required for LIMIT orders.")
            if price <= 0:
                raise ValueError(f"Limit price must be positive, got {price}.")
        else:
            # MARKET order
            if price is not None:
                price = None

        # Map Side
        if side == OrderSide.BUY:
            action = "BUY"
        elif side == OrderSide.SELL:
            action = "SELL"
        else:
            raise ValueError(f"Unsupported order side: {side}")

        # Construct Contract
        contract = Contract()
        contract.symbol = symbol
        contract.secType = self._settings.ibkr_market_data_sec_type
        contract.exchange = self._settings.ibkr_market_data_exchange
        contract.currency = self._settings.ibkr_market_data_currency
        if self._settings.ibkr_market_data_primary_exchange:
            contract.primaryExch = self._settings.ibkr_market_data_primary_exchange

        # Construct IBKR Order
        ib_order = IBOrder()
        ib_order.action = action
        ib_order.totalQuantity = float(quantity)

        if order_type_upper == "LIMIT" and price is not None:
            ib_order.orderType = "LMT"
            ib_order.lmtPrice = float(price)
        else:
            ib_order.orderType = "MKT"

        ib_order.transmit = True

        # Disable deprecated order attributes that trigger TWS error 10268
        ib_order.eTradeOnly = False
        ib_order.firmQuoteOnly = False

        # Get next valid order ID
        tws_order_id = self._get_next_order_id()
        order_id_str = str(tws_order_id)

        # Construct our domain model Order initially in PENDING state
        domain_order = Order(
            order_id=order_id_str,
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type_upper,
            price=price,
            status=OrderStatus.PENDING,
        )

        with self._lock:
            self._orders[order_id_str] = domain_order

        # Register request ID as an order under the lock
        self._client.register_request_id(tws_order_id, "order")

        logger.info(
            "Placing TWS Order: id=%d, symbol=%s, action=%s, qty=%d, type=%s, price=%s",
            tws_order_id,
            symbol,
            action,
            quantity,
            ib_order.orderType,
            price,
        )

        # Place the order through client outside the lock with cleanup on error
        try:
            self._client.placeOrder(tws_order_id, contract, ib_order)
        except Exception:
            with self._lock:
                if (
                    order_id_str in self._orders
                    and self._orders[order_id_str].status == OrderStatus.PENDING
                ):
                    self._orders.pop(order_id_str, None)
                    self._client.unregister_request_id(tws_order_id)
            raise

        return copy.copy(domain_order)

    async def modify_order(
        self,
        order_id: str,
        quantity: int | None = None,
        price: Decimal | None = None,
    ) -> Order:
        """Modify an existing open order in TWS by resubmitting with same ID."""
        self._require_connected()

        # Find the existing order
        with self._lock:
            if order_id not in self._orders:
                raise ValueError(f"Order not found: {order_id}")
            domain_order = self._orders[order_id]

            # Check if order is terminal
            if domain_order.status in (
                OrderStatus.FILLED,
                OrderStatus.CANCELLED,
                OrderStatus.REJECTED,
            ):
                raise ValueError(
                    f"Cannot modify order in terminal state: {domain_order.status.value}"
                )

            # Validate modifications
            # Quantity must be positive if supplied
            if quantity is not None:
                if quantity <= 0:
                    raise ValueError(f"Quantity must be positive, got {quantity}.")
                new_qty = quantity
            else:
                new_qty = domain_order.quantity

            # Price validation
            new_price: Decimal | None = None
            if domain_order.order_type == "LIMIT":
                if price is not None:
                    if price <= 0:
                        raise ValueError(f"Limit price must be positive, got {price}.")
                    new_price = price
                else:
                    new_price = domain_order.price
            else:
                # Market order cannot have price modification
                if price is not None:
                    raise ValueError("Cannot set price for a MARKET order.")
                new_price = None

            # Construct contract
            contract = Contract()
            contract.symbol = domain_order.symbol
            contract.secType = self._settings.ibkr_market_data_sec_type
            contract.exchange = self._settings.ibkr_market_data_exchange
            contract.currency = self._settings.ibkr_market_data_currency
            if self._settings.ibkr_market_data_primary_exchange:
                contract.primaryExch = self._settings.ibkr_market_data_primary_exchange

            # Construct modified TWS Order
            ib_order = IBOrder()
            ib_order.action = "BUY" if domain_order.side == OrderSide.BUY else "SELL"
            ib_order.totalQuantity = float(new_qty)

            if domain_order.order_type == "LIMIT" and new_price is not None:
                ib_order.orderType = "LMT"
                ib_order.lmtPrice = float(new_price)
            else:
                ib_order.orderType = "MKT"

            ib_order.transmit = True

            # Disable deprecated order attributes that trigger TWS error 10268
            ib_order.eTradeOnly = False
            ib_order.firmQuoteOnly = False

        # Place the order through client outside the lock using same order ID
        tws_order_id = int(order_id)
        logger.info(
            "Modifying TWS Order: id=%d, symbol=%s, qty=%d, price=%s",
            tws_order_id,
            domain_order.symbol,
            new_qty,
            new_price,
        )
        self._client.placeOrder(tws_order_id, contract, ib_order)

        return copy.copy(domain_order)

    async def cancel_order(self, order_id: str) -> Order:
        """Submit a cancel request to TWS for an existing open order."""
        self._require_connected()

        with self._lock:
            if order_id not in self._orders:
                raise ValueError(f"Order not found: {order_id}")
            domain_order = self._orders[order_id]

            if domain_order.status in (
                OrderStatus.FILLED,
                OrderStatus.CANCELLED,
                OrderStatus.REJECTED,
            ):
                raise ValueError(
                    f"Cannot cancel order in terminal state: {domain_order.status.value}"
                )

        tws_order_id = int(order_id)
        logger.info("Canceling TWS Order: id=%d", tws_order_id)

        # Submit cancel request outside the lock
        self._client.cancelOrder(tws_order_id)

        with self._lock:
            return copy.copy(domain_order)

    async def get_order_book(self) -> list[Order]:
        """Return copies of all tracked orders."""
        self._require_connected()
        with self._lock:
            return [copy.copy(o) for o in self._orders.values()]

    # ── EWrapper general listeners callbacks ─────────────────────────

    def on_position(
        self, account: str, contract: Any, position: float, avgCost: float
    ) -> None:
        """Handle position update callbacks from TWSClient."""
        with self._lock:
            if self._positions_future is None:
                return

            symbol = contract.symbol
            qty = int(position)
            avg_p = Decimal(str(avgCost))

            pos = Position(
                symbol=symbol,
                quantity=qty,
                average_price=avg_p,
            )
            self._collected_positions.append(pos)

    def on_position_end(self) -> None:
        """Handle position retrieval completion callback from TWSClient."""
        future_to_resolve = None
        loop_to_use = None
        result: list[Position] = []

        with self._lock:
            if self._positions_future is not None:
                future_to_resolve = self._positions_future
                loop_to_use = self._positions_loop
                # Return only non-flat positions to match MockBroker
                result = [p for p in self._collected_positions if not p.is_flat]

                # Reset state
                self._positions_future = None
                self._positions_loop = None
                self._collected_positions = []

        # Cancel positions subscription after completion
        try:
            self._client.cancelPositions()
        except Exception as e:  # noqa: BLE001
            logger.debug("Failed to cancel positions: %s", e)

        if future_to_resolve is not None and loop_to_use is not None:
            loop_to_use.call_soon_threadsafe(future_to_resolve.set_result, result)

    def on_account_summary(
        self, reqId: int, account: str, tag: str, value: str, currency: str
    ) -> None:
        """Handle account summary callbacks from TWSClient."""
        with self._lock:
            if reqId not in self._margin_futures:
                return

            if tag in ("NetLiquidation", "AvailableFunds", "BuyingPower"):
                try:
                    self._margin_data[reqId][tag] = Decimal(value)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "Failed to parse account summary tag %s value: %s, error: %s",
                        tag,
                        value,
                        e,
                    )

    def on_account_summary_end(self, reqId: int) -> None:
        """Handle account summary retrieval completion callback from TWSClient."""
        future_to_resolve = None
        loop_to_use = None
        exception_to_raise = None
        margin_result = None

        with self._lock:
            if reqId in self._margin_futures:
                future_to_resolve, loop_to_use = self._margin_futures.pop(reqId)
                data = self._margin_data.pop(reqId, {})

                required = ("NetLiquidation", "AvailableFunds", "BuyingPower")
                missing = [tag for tag in required if tag not in data]
                if missing:
                    exception_to_raise = ValueError(
                        f"Missing required account summary values for reqId {reqId}: {missing}"
                    )
                else:
                    margin_result = Margin(
                        equity=data["NetLiquidation"],
                        available_funds=data["AvailableFunds"],
                        buying_power=data["BuyingPower"],
                    )

        # Cancel account summary subscription for this request
        try:
            self._client.unregister_request_id(reqId)
            self._client.cancelAccountSummary(reqId)
        except Exception as e:  # noqa: BLE001
            logger.debug("Failed to cancel account summary: %s", e)

        if future_to_resolve is not None and loop_to_use is not None:
            if exception_to_raise is not None:
                loop_to_use.call_soon_threadsafe(
                    future_to_resolve.set_exception, exception_to_raise
                )
            elif margin_result is not None:
                loop_to_use.call_soon_threadsafe(
                    future_to_resolve.set_result, margin_result
                )

    def on_error(self, reqId: int, errorCode: int, errorString: str) -> None:
        """Handle error callbacks from TWSClient."""
        margin_future_to_fail = None
        margin_loop = None
        positions_future_to_fail = None
        positions_loop = None

        # Resolve request type from TWSClient registry
        req_type = self._client.get_request_type(reqId)

        with self._lock:
            # If the error is specific to a margin request reqId
            if req_type == "margin" and reqId in self._margin_futures:
                margin_future_to_fail, margin_loop = self._margin_futures.pop(reqId)
                self._margin_data.pop(reqId, None)
                self._client.unregister_request_id(reqId)

            # If the error is connection level or a general error (reqId is -1 or matches lost connection)
            if (
                reqId == -1 or not self._client.is_connected()
            ) and self._positions_future is not None:
                positions_future_to_fail = self._positions_future
                positions_loop = self._positions_loop
                self._positions_future = None
                self._positions_loop = None
                self._collected_positions = []

            # Order error correlation: only process if the request type is confirmed as "order"
            if req_type == "order":
                order_id_str = str(reqId)
                if order_id_str in self._orders:
                    domain_order = self._orders[order_id_str]
                    if domain_order.status not in (
                        OrderStatus.FILLED,
                        OrderStatus.CANCELLED,
                        OrderStatus.REJECTED,
                    ):
                        if errorCode == 202:
                            domain_order.status = OrderStatus.CANCELLED
                        else:
                            domain_order.status = OrderStatus.REJECTED
                        logger.warning(
                            "Order %s transitioned to %s due to TWS error %d: %s",
                            order_id_str,
                            domain_order.status.value,
                            errorCode,
                            errorString,
                        )

        exc = RuntimeError(f"TWS error reqId={reqId} code={errorCode}: {errorString}")

        if margin_future_to_fail is not None and margin_loop is not None:
            margin_loop.call_soon_threadsafe(margin_future_to_fail.set_exception, exc)

        if positions_future_to_fail is not None and positions_loop is not None:
            positions_loop.call_soon_threadsafe(
                positions_future_to_fail.set_exception, exc
            )

    def on_connection_closed(self) -> None:
        """Handle connection closed callbacks from TWSClient."""
        positions_future_to_fail = None
        positions_loop = None
        margin_futures_to_fail = []

        with self._lock:
            if self._positions_future is not None:
                positions_future_to_fail = self._positions_future
                positions_loop = self._positions_loop
                self._positions_future = None
                self._positions_loop = None
                self._collected_positions = []

            for req_id, (fut, loop) in list(self._margin_futures.items()):
                margin_futures_to_fail.append((fut, loop))
            self._margin_futures.clear()
            self._margin_data.clear()

        exc = ConnectionError("TWS connection closed during request.")

        if positions_future_to_fail is not None and positions_loop is not None:
            positions_loop.call_soon_threadsafe(
                positions_future_to_fail.set_exception, exc
            )

        for fut, loop in margin_futures_to_fail:
            loop.call_soon_threadsafe(fut.set_exception, exc)

    def on_open_order(
        self, orderId: int, contract: Any, order: Any, orderState: Any
    ) -> None:
        """Handle openOrder callbacks from TWSClient."""
        order_id_str = str(orderId)
        with self._lock:
            if order_id_str in self._orders:
                domain_order = self._orders[order_id_str]

                # Update quantity and price from confirmed TWS state (P1-2)
                domain_order.quantity = int(order.totalQuantity)
                if order.orderType == "LMT":
                    domain_order.price = Decimal(str(order.lmtPrice))

                ib_status = orderState.status
                new_status = self._map_status(ib_status)

                # Status protection (P0-1)
                is_currently_terminal = domain_order.status in (
                    OrderStatus.FILLED,
                    OrderStatus.CANCELLED,
                    OrderStatus.REJECTED,
                )
                if not is_currently_terminal:
                    domain_order.status = new_status

                logger.info(
                    "Order openOrder status update: id=%s, status=%s (mapped from TWS: %s)",
                    order_id_str,
                    domain_order.status.value,
                    ib_status,
                )

    def on_order_status(
        self,
        orderId: int,
        status: str,
        filled: float,
        remaining: float,
        avgFillPrice: float,
        permId: int,
        parentId: int,
        lastFillPrice: float,
        clientId: int,
        whyHeld: str,
        mktCapPrice: float,
    ) -> None:
        """Handle orderStatus callbacks from TWSClient."""
        order_id_str = str(orderId)
        with self._lock:
            if order_id_str in self._orders:
                domain_order = self._orders[order_id_str]
                new_status = self._map_status(status)

                qty_filled = int(filled)
                qty_remaining = int(remaining)

                # Status protection (P0-1)
                is_currently_terminal = domain_order.status in (
                    OrderStatus.FILLED,
                    OrderStatus.CANCELLED,
                    OrderStatus.REJECTED,
                )
                if not is_currently_terminal:
                    if qty_filled > 0 and qty_remaining > 0:
                        domain_order.status = OrderStatus.PARTIALLY_FILLED
                    else:
                        domain_order.status = new_status

                # Update execution info monotonically: a stale/out-of-order callback
                # must never regress already-recorded fill data (P0-1). Only a higher
                # cumulative filled quantity advances filled_quantity / avg price.
                if qty_filled > domain_order.filled_quantity:
                    domain_order.filled_quantity = qty_filled
                    if avgFillPrice > 0:
                        domain_order.average_fill_price = Decimal(str(avgFillPrice))

                logger.info(
                    "Order orderStatus update: id=%s, status=%s, filled=%d, avgPrice=%s",
                    order_id_str,
                    domain_order.status.value,
                    qty_filled,
                    avgFillPrice,
                )

    def on_open_order_end(self) -> None:
        """Handle openOrderEnd callbacks from TWSClient."""
        logger.debug("Received openOrderEnd from TWS.")
