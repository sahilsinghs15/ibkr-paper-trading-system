"""IBKR Broker Adapter implementation."""

import asyncio
import logging
import threading
from decimal import Decimal
from typing import Any

from app.broker.base_broker import BaseBroker
from app.broker.ibkr.tws_client import TWSClient
from app.core.config import Settings
from app.models.broker import Margin
from app.models.order import Order, OrderSide
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

        # Counter for reqAccountSummary IDs
        self._req_id_counter = 1000

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

        # Call reqAccountSummary outside the lock
        tags = "NetLiquidation,AvailableFunds,BuyingPower"
        logger.info("Requesting account summary for reqId=%d...", req_id)
        self._client.reqAccountSummary(req_id, "All", tags)

        try:
            return await asyncio.wait_for(future, timeout=self._timeout)
        except TimeoutError:
            logger.error("Margin request timed out for reqId=%d.", req_id)
            with self._lock:
                self._margin_futures.pop(req_id, None)
                self._margin_data.pop(req_id, None)
            try:
                self._client.cancelAccountSummary(req_id)
            except Exception as e:  # noqa: BLE001
                logger.debug("Failed to cancel account summary: %s", e)
            raise TimeoutError("Margin request timed out.")
        except Exception:
            with self._lock:
                self._margin_futures.pop(req_id, None)
                self._margin_data.pop(req_id, None)
            try:
                self._client.cancelAccountSummary(req_id)
            except Exception as e:  # noqa: BLE001
                logger.debug("Failed to cancel account summary: %s", e)
            raise

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: int,
        order_type: str,
        price: Decimal | None = None,
    ) -> Order:
        """Unimplemented in this read-only phase."""
        raise NotImplementedError("Order placement is not supported in this phase.")

    async def modify_order(
        self,
        order_id: str,
        quantity: int | None = None,
        price: Decimal | None = None,
    ) -> Order:
        """Unimplemented in this read-only phase."""
        raise NotImplementedError("Order modification is not supported in this phase.")

    async def cancel_order(self, order_id: str) -> Order:
        """Unimplemented in this read-only phase."""
        raise NotImplementedError("Order cancellation is not supported in this phase.")

    async def get_order_book(self) -> list[Order]:
        """Unimplemented in this read-only phase."""
        raise NotImplementedError(
            "Order book retrieval is not supported in this phase."
        )

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

        with self._lock:
            # If the error is specific to a margin request reqId
            if reqId in self._margin_futures:
                margin_future_to_fail, margin_loop = self._margin_futures.pop(reqId)
                self._margin_data.pop(reqId, None)

            # If the error is connection level or a general error (reqId is -1 or matches lost connection)
            if (
                reqId == -1 or not self._client.is_connected()
            ) and self._positions_future is not None:
                positions_future_to_fail = self._positions_future
                positions_loop = self._positions_loop
                self._positions_future = None
                self._positions_loop = None
                self._collected_positions = []

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
