"""IBKR TWS API connection client and wrapper."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING, Any

from ibapi.client import EClient  # type: ignore[import-untyped]
from ibapi.wrapper import EWrapper  # type: ignore[import-untyped]

from app.broker.ibkr.positions import BrokerPositionLine, PositionSnapshotCollector

if TYPE_CHECKING:
    from app.broker.ibkr.gateway_rate_limiter import GatewayRateLimiter

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
        self._listeners: list[Any] = []

        self._request_types: dict[int, str] = {}
        self._registry_lock = threading.Lock()

        self._contract_details_lock = threading.Lock()
        self._contract_details_events: dict[int, threading.Event] = {}
        self._contract_details_results: dict[int, list[Any]] = {}
        self._next_contract_details_req = 60000

        self._positions_request_lock = threading.Lock()
        self._position_collector = PositionSnapshotCollector()
        self._rate_limiter: GatewayRateLimiter | None = None

    def register_rate_limiter(self, limiter: GatewayRateLimiter) -> None:
        """Attach the shared gateway rate limiter for outbound API pacing."""
        self._rate_limiter = limiter

    def register_request_id(self, req_id: int, req_type: str) -> None:
        """Register a request ID with its type under lock."""
        with self._registry_lock:
            self._request_types[req_id] = req_type

    def unregister_request_id(self, req_id: int) -> None:
        """Unregister a request ID under lock."""
        with self._registry_lock:
            self._request_types.pop(req_id, None)

    def get_request_type(self, req_id: int) -> str | None:
        """Retrieve the registered type of a request ID under lock."""
        with self._registry_lock:
            return self._request_types.get(req_id)

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
        # IBKR status codes 2000-2999 are informational notifications/warnings
        # and do not indicate actual failure.
        if errorCode >= 2000 and errorCode < 3000:
            if errorCode == 2176:
                logger.debug(
                    "TWS Status Notification: reqId=%d, code=%d, message=%s",
                    reqId,
                    errorCode,
                    errorString,
                )
            else:
                logger.info(
                    "TWS Status Notification: reqId=%d, code=%d, message=%s",
                    reqId,
                    errorCode,
                    errorString,
                )
        else:
            if errorCode == 100 and self._rate_limiter is not None:
                self._rate_limiter.notify_error_100()
            logger.warning(
                "TWS API Error: reqId=%d, code=%d, message=%s",
                reqId,
                errorCode,
                errorString,
            )
            for listener in list(self._market_data_listeners):
                try:
                    if hasattr(listener, "on_error"):
                        listener.on_error(reqId, errorCode, errorString)
                except Exception:
                    logger.exception("Error in market_data_listener on_error callback")
            for listener in list(self._listeners):
                try:
                    listener.on_error(reqId, errorCode, errorString)
                except AttributeError:
                    pass
                except Exception:
                    logger.exception("Error in onError listener callback")

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
        for listener in list(self._listeners):
            try:
                listener.on_connection_closed()
            except AttributeError:
                pass
            except Exception:
                logger.exception("Error in connectionClosed general listener callback")

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

    def rerouteMktDataReq(self, reqId: int, conId: int, exchange: str) -> None:
        """Callback received when IBKR reroutes CFD market data request to underlying conId."""
        super().rerouteMktDataReq(reqId, conId, exchange)
        logger.info("TWSClient rerouteMktDataReq: reqId=%d underlying_conId=%d exchange=%s", reqId, conId, exchange)
        for listener in list(self._market_data_listeners):
            try:
                if hasattr(listener, "on_reroute_mkt_data"):
                    listener.on_reroute_mkt_data(reqId, conId, exchange)
            except Exception:
                logger.exception("Error in rerouteMktDataReq listener callback")

    def contractDetails(self, reqId: int, contractDetails: Any) -> None:
        """Callback receiving a single ContractDetails result."""
        super().contractDetails(reqId, contractDetails)
        with self._contract_details_lock:
            if reqId not in self._contract_details_events:
                return
            self._contract_details_results.setdefault(reqId, []).append(contractDetails)
        for listener in list(self._listeners):
            try:
                listener.on_contract_details(reqId, contractDetails)
            except AttributeError:
                pass
            except Exception:
                logger.exception("Error in contractDetails listener callback")

    def contractDetailsEnd(self, reqId: int) -> None:
        """Callback indicating contract details transmission is complete."""
        super().contractDetailsEnd(reqId)
        with self._contract_details_lock:
            event = self._contract_details_events.get(reqId)
            if event is not None:
                event.set()
        for listener in list(self._listeners):
            try:
                listener.on_contract_details_end(reqId)
            except AttributeError:
                pass
            except Exception:
                logger.exception("Error in contractDetailsEnd listener callback")

    def accountSummary(
        self, reqId: int, account: str, tag: str, value: str, currency: str
    ) -> None:
        """Callback received when an account summary value is updated."""
        super().accountSummary(reqId, account, tag, value, currency)
        for listener in list(self._listeners):
            try:
                listener.on_account_summary(reqId, account, tag, value, currency)
            except AttributeError:
                pass
            except Exception:
                logger.exception("Error in accountSummary listener callback")

    def accountSummaryEnd(self, reqId: int) -> None:
        """Callback received when account summary values transmission is complete."""
        super().accountSummaryEnd(reqId)
        for listener in list(self._listeners):
            try:
                listener.on_account_summary_end(reqId)
            except AttributeError:
                pass
            except Exception:
                logger.exception("Error in accountSummaryEnd listener callback")

    def position(
        self, account: str, contract: Any, position: float, avgCost: float
    ) -> None:
        """Callback received when a position update is reported."""
        super().position(account, contract, position, avgCost)
        for listener in list(self._listeners):
            try:
                listener.on_position(account, contract, position, avgCost)
            except AttributeError:
                pass
            except Exception:
                logger.exception("Error in position listener callback")

    def positionEnd(self) -> None:
        """Callback received when position updates transmission is complete."""
        super().positionEnd()
        for listener in list(self._listeners):
            try:
                listener.on_position_end()
            except AttributeError:
                pass
            except Exception:
                logger.exception("Error in positionEnd listener callback")

    def openOrder(
        self, orderId: int, contract: Any, order: Any, orderState: Any
    ) -> None:
        """Callback received when an open order details are updated."""
        super().openOrder(orderId, contract, order, orderState)
        for listener in list(self._listeners):
            try:
                listener.on_open_order(orderId, contract, order, orderState)
            except AttributeError:
                pass
            except Exception:
                logger.exception("Error in openOrder listener callback")

    def orderStatus(
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
        """Callback received when an order's execution status is updated."""
        super().orderStatus(
            orderId,
            status,
            filled,
            remaining,
            avgFillPrice,
            permId,
            parentId,
            lastFillPrice,
            clientId,
            whyHeld,
            mktCapPrice,
        )
        for listener in list(self._listeners):
            try:
                listener.on_order_status(
                    orderId,
                    status,
                    filled,
                    remaining,
                    avgFillPrice,
                    permId,
                    parentId,
                    lastFillPrice,
                    clientId,
                    whyHeld,
                    mktCapPrice,
                )
            except AttributeError:
                pass
            except Exception:
                logger.exception("Error in orderStatus listener callback")

    def openOrderEnd(self) -> None:
        """Callback received when open orders transmission is complete."""
        super().openOrderEnd()
        for listener in list(self._listeners):
            try:
                listener.on_open_order_end()
            except AttributeError:
                pass
            except Exception:
                logger.exception("Error in openOrderEnd listener callback")

    def execDetails(self, reqId: int, contract: Any, execution: Any) -> None:
        """Callback received when an order execution/fill details update."""
        super().execDetails(reqId, contract, execution)
        for listener in list(self._listeners):
            try:
                listener.on_exec_details(reqId, contract, execution)
            except AttributeError:
                pass
            except Exception:
                logger.exception("Error in execDetails listener callback")

    def execDetailsEnd(self, reqId: int) -> None:
        """Callback received when execution details transmission is complete."""
        super().execDetailsEnd(reqId)
        for listener in list(self._listeners):
            try:
                listener.on_exec_details_end(reqId)
            except AttributeError:
                pass
            except Exception:
                logger.exception("Error in execDetailsEnd listener callback")

    def commissionReport(self, commissionReport: Any) -> None:
        """Callback received when commission info is reported for an execution."""
        super().commissionReport(commissionReport)
        for listener in list(self._listeners):
            try:
                listener.on_commission_report(commissionReport)
            except AttributeError:
                pass
            except Exception:
                logger.exception("Error in commissionReport listener callback")

    def register_market_data_listener(self, listener: Any) -> None:
        """Register a listener to receive market data events."""
        self._market_data_listeners.append(listener)

    def register_listener(self, listener: Any) -> None:
        """Register a general listener to receive TWS wrapper callbacks."""
        self._listeners.append(listener)

    def request_contract_details(
        self, contract: Any, *, timeout: float = 5.0
    ) -> list[Any]:
        """Request IBKR contract details and block until complete or timeout."""
        if not self.is_connected():
            logger.warning("request_contract_details: TWS not connected")
            return []
        if self._rate_limiter is not None:
            from app.broker.ibkr.gateway_rate_limiter import PRIORITY_CONTRACT_DETAILS

            acquired = self._rate_limiter.blocking_acquire(
                PRIORITY_CONTRACT_DETAILS,
                "reqContractDetails",
                timeout=min(timeout, self._rate_limiter.max_wait_sec),
            )
            if acquired is None:
                logger.warning(
                    "request_contract_details: gateway pacing timeout reqContractDetails"
                )
                return []
        with self._contract_details_lock:
            req_id = self._next_contract_details_req
            self._next_contract_details_req += 1
            event = threading.Event()
            self._contract_details_events[req_id] = event
            self._contract_details_results[req_id] = []
        self.register_request_id(req_id, "contract_details")
        try:
            self.reqContractDetails(req_id, contract)
        except Exception:
            logger.exception("reqContractDetails failed for reqId=%d", req_id)
            self._clear_contract_details_request(req_id)
            return []
        completed = event.wait(timeout=timeout)
        with self._contract_details_lock:
            results = list(self._contract_details_results.get(req_id, []))
        self._clear_contract_details_request(req_id)
        if not completed:
            logger.warning(
                "request_contract_details timed out after %.1fs reqId=%d",
                timeout,
                req_id,
            )
        return results

    async def request_contract_details_async(
        self, contract: Any, *, timeout: float = 5.0
    ) -> list[Any]:
        """Request IBKR contract details asynchronously off the event loop."""
        return await asyncio.to_thread(self.request_contract_details, contract, timeout=timeout)

    def request_positions(self, *, timeout: float = 15.0) -> tuple[list[BrokerPositionLine], bool]:
        """Request IBKR positions and block until positionEnd or timeout.

        Returns (lines, timed_out). Lines may be partial when timed_out is True.
        """
        if not self.is_connected():
            logger.warning("request_positions: TWS not connected")
            return [], False

        with self._positions_request_lock:
            collector = self._position_collector
            collector.reset()
            if collector not in self._listeners:
                self.register_listener(collector)

            try:
                cancel = getattr(self, "cancelPositions", None)
                if callable(cancel):
                    try:
                        cancel()
                    except Exception:
                        logger.exception("cancelPositions before snapshot failed")

                self.reqPositions()
                completed = collector.wait(timeout=timeout)
                lines = collector.snapshot()

                if callable(cancel):
                    try:
                        cancel()
                    except Exception:
                        logger.exception("cancelPositions after snapshot failed")

                if not completed:
                    logger.warning(
                        "request_positions timed out after %.1fs; returning %d line(s)",
                        timeout,
                        len(lines),
                    )
                return lines, not completed
            except Exception:
                logger.exception("request_positions failed")
                return collector.snapshot(), True

    async def request_positions_async(
        self, *, timeout: float = 15.0
    ) -> tuple[list[BrokerPositionLine], bool]:
        """Request IBKR positions asynchronously off the event loop."""
        return await asyncio.to_thread(self.request_positions, timeout=timeout)

    def _clear_contract_details_request(self, req_id: int) -> None:
        with self._contract_details_lock:
            self._contract_details_events.pop(req_id, None)
            self._contract_details_results.pop(req_id, None)
        self.unregister_request_id(req_id)

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
        with self._registry_lock:
            self._request_types.clear()
        with self._contract_details_lock:
            for event in self._contract_details_events.values():
                event.set()
            self._contract_details_events.clear()
            self._contract_details_results.clear()
        logger.info("TWS disconnected.")
