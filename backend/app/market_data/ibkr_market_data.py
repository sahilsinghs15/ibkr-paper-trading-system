"""IBKR Market Data Adapter translating TWS callbacks to domain events."""

import logging
import queue
import threading
from datetime import UTC, datetime
from decimal import Decimal

from ibapi.contract import Contract  # type: ignore[import-untyped]

from app.broker.ibkr.tws_client import TWSClient
from app.core.config import Settings
from app.models.market_data import MarketDataEvent

logger = logging.getLogger(__name__)


class IBKRMarketDataAdapter:
    """Subscribes to market data via TWS Client and publishes normalized events.

    Listens to TWS callbacks (tickPrice, tickSize, marketDataType) and publishes
    thread-safe MarketDataEvent instances to a queue.

    Thread Safety:
    - Thread-safe via a dedicated re-entrant or standard Lock (`self._lock`) for adapter state.
    - Locks are NOT held during blocking operations (queue.put, TWS EClient calls).
    - Callback iteration snapshots protect listener lists in TWSClient.
    """

    def __init__(self, client: TWSClient, settings: Settings) -> None:
        """Initialize adapter with client, configuration, and event queue."""
        self._client = client
        self._settings = settings
        self._queue: queue.Queue[MarketDataEvent] = queue.Queue()
        self._req_id_counter = (
            10000000  # Unique ID counter for TWS requests (using high base)
        )
        self._lock = threading.Lock()

        # Active subscription tracking
        self._active_req_id: int | None = None
        self._active_symbol: str | None = None

        # Pairing cache for tickPrice and tickSize calls
        self._last_price: Decimal | None = None
        self._last_price_time: datetime | None = None
        self._last_volume: int | None = None

        # Register this adapter as a callback listener on the TWS client
        self._client.register_market_data_listener(self)

    # ── Client callback listeners ───────────────────────────────────

    def on_tick_price(self, reqId: int, tickType: int, price: float) -> None:
        """Invoked when TWS reports a price update.

        We filter for LAST (4) and DELAYED_LAST (68) tick types.
        """
        if tickType not in (4, 68):
            return

        event_to_publish: MarketDataEvent | None = None
        pending_event: MarketDataEvent | None = None

        with self._lock:
            if reqId != self._active_req_id:
                return

            if price <= 0:
                logger.warning(
                    "Ignored non-positive price callback: reqId=%d, type=%d, price=%f",
                    reqId,
                    tickType,
                    price,
                )
                return

            # Flush any pending un-paired price update to prevent losing it
            if self._last_price is not None:
                pending_event = MarketDataEvent(
                    timestamp=self._last_price_time or datetime.now(UTC),
                    price=self._last_price,
                    volume=0,
                )

            # Update cached price and price observation time
            self._last_price = Decimal(str(price))
            self._last_price_time = datetime.now(UTC)

            # If size tick arrived first, pair immediately and emit
            if self._last_volume is not None:
                event_to_publish = MarketDataEvent(
                    timestamp=self._last_price_time,
                    price=self._last_price,
                    volume=self._last_volume,
                )
                # Reset cache
                self._last_price = None
                self._last_price_time = None
                self._last_volume = None

        # Publish events OUTSIDE the lock
        if pending_event is not None:
            self._queue.put(pending_event)
            logger.debug("Flushed pending price event: %s", pending_event)

        if event_to_publish is not None:
            self._queue.put(event_to_publish)
            logger.debug(
                "Normalized MarketDataEvent (paired size-first): %s",
                event_to_publish,
            )

    def on_tick_size(self, reqId: int, tickType: int, size: int) -> None:
        """Invoked when TWS reports a size/volume update.

        We filter for LAST_SIZE (5) and DELAYED_LAST_SIZE (71) tick types.
        """
        if tickType not in (5, 71):
            return

        event_to_publish: MarketDataEvent | None = None

        with self._lock:
            if reqId != self._active_req_id:
                return

            if size < 0:
                logger.warning(
                    "Ignored negative size callback: reqId=%d, type=%d, size=%d",
                    reqId,
                    tickType,
                    size,
                )
                return

            self._last_volume = size

            # If price was already received, pair immediately and emit
            if self._last_price is not None:
                event_to_publish = MarketDataEvent(
                    timestamp=self._last_price_time or datetime.now(UTC),
                    price=self._last_price,
                    volume=self._last_volume,
                )
                # Reset cache
                self._last_price = None
                self._last_price_time = None
                self._last_volume = None

        # Publish event OUTSIDE the lock
        if event_to_publish is not None:
            self._queue.put(event_to_publish)
            logger.debug(
                "Normalized MarketDataEvent (paired price-first): %s",
                event_to_publish,
            )

    def on_market_data_type(self, reqId: int, marketDataType: int) -> None:
        """Invoked when TWS reports the active market data type status."""
        with self._lock:
            if reqId != self._active_req_id:
                return

        logger.info(
            "TWS confirmed market data type for reqId=%d is: %d (1=Live, 2=Frozen, 3=Delayed, 4=Delayed Frozen)",
            reqId,
            marketDataType,
        )

    def on_connection_closed(self) -> None:
        """Invoked when TWS connection drops unexpectedly or closes."""
        pending_event: MarketDataEvent | None = None

        with self._lock:
            logger.warning(
                "TWS connection closed. Flushing pending events and clearing subscription state."
            )
            if self._last_price is not None:
                pending_event = MarketDataEvent(
                    timestamp=self._last_price_time or datetime.now(UTC),
                    price=self._last_price,
                    volume=0,
                )

            # Clear state
            self._active_req_id = None
            self._active_symbol = None
            self._last_price = None
            self._last_price_time = None
            self._last_volume = None

        # Publish pending event OUTSIDE the lock
        if pending_event is not None:
            try:
                self._queue.put(pending_event)
                logger.info(
                    "Flushed pending price event on connectionClosed: %s",
                    pending_event,
                )
            except Exception:
                logger.exception("Error flushing pending price on connection closed")

    # ── Subscription Lifecycle Management ────────────────────────────

    def request_market_data(self) -> int:
        """Subscribe to TWS market data for the configured contract.

        Maintains idempotency; returns existing request ID if subscription is active.

        Returns:
            The unique request ID assigned to this TWS subscription.
        """
        if not self._client.is_connected():
            raise RuntimeError(
                "Cannot request market data: TWS client is not connected."
            )

        with self._lock:
            if self._active_req_id is not None:
                logger.info(
                    "Market data subscription already active for reqId=%d. No new request submitted.",
                    self._active_req_id,
                )
                return self._active_req_id

            # Construct contract and request parameters
            contract = self._create_contract()

            self._last_price = None
            self._last_price_time = None
            self._last_volume = None

            req_id = self._req_id_counter
            self._req_id_counter += 1

            self._active_req_id = req_id
            self._active_symbol = contract.symbol

            # Register request ID namespace ownership
            self._client.register_request_id(req_id, "market_data")

        # Perform socket calls and configuration updates OUTSIDE the lock!
        mkt_data_type = self._settings.ibkr_market_data_type
        logger.info(
            "Setting TWS market data type to %d (reqId=%d)",
            mkt_data_type,
            req_id,
        )
        self._client.reqMarketDataType(mkt_data_type)

        logger.info(
            "Subscribing to TWS market data: Symbol=%s, SecType=%s, Exchange=%s, Currency=%s (reqId=%d)",
            contract.symbol,
            contract.secType,
            contract.exchange,
            contract.currency,
            req_id,
        )

        # Call EClient method
        self._client.reqMktData(req_id, contract, "", False, False, [])

        logger.info("Market data subscription requested successfully.")
        return req_id

    def cancel_market_data(self) -> None:
        """Cancel the active TWS market data subscription."""
        req_id_to_cancel: int | None = None
        symbol_to_cancel: str | None = None
        pending_event: MarketDataEvent | None = None

        with self._lock:
            if self._active_req_id is None:
                logger.info("No active market data subscription to cancel.")
                return

            req_id_to_cancel = self._active_req_id
            symbol_to_cancel = self._active_symbol

            # Flush any pending un-paired price update to prevent losing it
            if self._last_price is not None:
                pending_event = MarketDataEvent(
                    timestamp=self._last_price_time or datetime.now(UTC),
                    price=self._last_price,
                    volume=0,
                )

            # Clear state
            self._active_req_id = None
            self._active_symbol = None
            self._last_price = None
            self._last_price_time = None
            self._last_volume = None

        # Publish flushed event OUTSIDE the lock
        if pending_event is not None:
            self._queue.put(pending_event)
            logger.info(
                "Flushed pending price event on cancellation: %s", pending_event
            )

        # Call TWS cancel OUTSIDE the lock
        if req_id_to_cancel is not None:
            logger.info(
                "Canceling TWS market data subscription for reqId=%d...",
                req_id_to_cancel,
            )
            self._client.unregister_request_id(req_id_to_cancel)
            self._client.cancelMktData(req_id_to_cancel)
            logger.info(
                "Subscription canceled: reqId=%d, symbol=%s.",
                req_id_to_cancel,
                symbol_to_cancel,
            )

    def get_event(self, timeout: float = 1.0) -> MarketDataEvent | None:
        """Fetch a normalized MarketDataEvent from the queue (blocks up to timeout).

        Args:
            timeout: Max seconds to block waiting for an event.

        Returns:
            The normalized MarketDataEvent or None if queue was empty.
        """
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def queue_size(self) -> int:
        """Return current size of the normalized event queue."""
        return self._queue.qsize()

    # ── Helpers ──────────────────────────────────────────────────────

    def _create_contract(self) -> Contract:
        """Build an IBKR Contract instance using settings configuration."""
        contract = Contract()
        contract.symbol = self._settings.ibkr_market_data_symbol
        contract.secType = self._settings.ibkr_market_data_sec_type
        contract.exchange = self._settings.ibkr_market_data_exchange
        contract.currency = self._settings.ibkr_market_data_currency
        if self._settings.ibkr_market_data_primary_exchange:
            contract.primaryExch = self._settings.ibkr_market_data_primary_exchange
        return contract
