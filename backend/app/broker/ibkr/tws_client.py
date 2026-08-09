"""IBKR TWS API connection client and wrapper."""

import logging
import threading
from typing import Any

from ibapi.client import EClient  # type: ignore[import-untyped]
from ibapi.wrapper import EWrapper  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


class TWSClient(EWrapper, EClient):
    """Client adapter connecting the Python application to TWS Demo or Gateway.

    Inherits from EWrapper (for receiving callbacks) and EClient (for sending requests).
    Runs the TWS message loop in a separate background daemon thread to avoid
    blocking the main FastAPI execution thread.
    """

    def __init__(self) -> None:
        """Initialize EWrapper, EClient, and connection state variables."""
        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)

        self.next_order_id: int | None = None
        self._connected_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._market_data_listeners: list[Any] = []

    # ── EWrapper Callbacks ───────────────────────────────────────────

    def nextValidId(self, orderId: int) -> None:
        """Callback received when initial handshake finishes.

        Indicates the connection is ready to accept commands.
        """
        super().nextValidId(orderId)
        self.next_order_id = orderId
        self._connected_event.set()
        logger.info(
            "TWS nextValidId received: next_order_id=%d. Handshake complete.",
            orderId,
        )

    def error(
        self,
        reqId: int,
        errorCode: int,
        errorString: str,
        advancedOrderRejectJson: str = "",
    ) -> None:
        """Callback received when TWS generates an error or status message."""
        # System-level message codes (like 2104, 2106, 2158) are informational/status notifications
        # and do not indicate actual failure.
        if errorCode >= 2000 and errorCode < 3000:
            logger.info(
                "TWS Status Notification: reqId=%d, code=%d, message=%s",
                reqId,
                errorCode,
                errorString,
            )
        else:
            logger.warning(
                "TWS API Error: reqId=%d, code=%d, message=%s",
                reqId,
                errorCode,
                errorString,
            )

    def connectionClosed(self) -> None:
        """Callback when TWS connection drops unexpectedly or closes."""
        logger.warning("TWS connection has been closed.")
        self._connected_event.clear()
        self.next_order_id = None
        for listener in list(self._market_data_listeners):
            try:
                listener.on_connection_closed()
            except Exception:
                logger.exception("Error in connectionClosed listener callback")

    def tickPrice(self, reqId: int, tickType: int, price: float, attrib: Any) -> None:
        """Callback received when a market price updates."""
        super().tickPrice(reqId, tickType, price, attrib)
        for listener in list(self._market_data_listeners):
            try:
                listener.on_tick_price(reqId, tickType, price)
            except Exception:
                logger.exception("Error in tickPrice listener callback")

    def tickSize(self, reqId: int, tickType: int, size: int) -> None:
        """Callback received when a market size/volume updates."""
        super().tickSize(reqId, tickType, size)
        for listener in list(self._market_data_listeners):
            try:
                listener.on_tick_size(reqId, tickType, size)
            except Exception:
                logger.exception("Error in tickSize listener callback")

    def marketDataType(self, reqId: int, marketDataType: int) -> None:
        """Callback received when the market data type updates."""
        super().marketDataType(reqId, marketDataType)
        for listener in list(self._market_data_listeners):
            try:
                listener.on_market_data_type(reqId, marketDataType)
            except Exception:
                logger.exception("Error in marketDataType listener callback")

    def register_market_data_listener(self, listener: Any) -> None:
        """Register a listener to receive market data events."""
        self._market_data_listeners.append(listener)

    # ── Connection Lifecycle Methods ─────────────────────────────────

    def is_connected(self) -> bool:
        """Return True if connection is active and the nextValidId handshake is complete."""
        return self._connected_event.is_set() and self.isConnected()

    def connect_and_start(
        self, host: str, port: int, client_id: int, timeout: float = 10.0
    ) -> bool:
        """Connect to TWS and spin up the internal client reader thread.

        Blocks until either the nextValidId handshake completes or the timeout expires.

        Args:
            host: Hostname or IP of TWS.
            port: Socket port of TWS.
            client_id: Unique client ID for this socket session.
            timeout: Maximum seconds to wait for nextValidId handshake.

        Returns:
            True if connection and handshake succeeded, False otherwise.
        """
        if self.is_connected():
            logger.warning("Already connected to TWS.")
            return True

        self._connected_event.clear()
        self.next_order_id = None

        logger.info(
            "Attempting TWS connection to %s:%d (clientID=%d)...",
            host,
            port,
            client_id,
        )

        try:
            self.connect(host, port, client_id)
        except OSError as e:
            logger.error("Failed to open TCP socket connection to TWS: %s", e)
            return False

        # Start the background reader thread
        self._thread = threading.Thread(
            target=self.run, name="TWSClientThread", daemon=True
        )
        self._thread.start()

        # Wait for nextValidId handshake
        handshake_completed = self._connected_event.wait(timeout=timeout)
        if handshake_completed:
            logger.info("TWS connection established and handshake completed.")
            return True
        else:
            logger.error(
                "TWS connection handshake timed out after %.1f seconds.",
                timeout,
            )
            self.disconnect_clean()
            return False

    def disconnect_clean(self) -> None:
        """Cleanly disconnect from TWS and join the background thread."""
        logger.info("Disconnecting cleanly from TWS...")
        self.disconnect()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            self._thread = None

        self._connected_event.clear()
        self.next_order_id = None
        logger.info("TWS disconnected.")
