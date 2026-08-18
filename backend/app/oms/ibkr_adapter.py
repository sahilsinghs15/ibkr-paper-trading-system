"""IBKR Execution Adapter implementation connecting OMS to IBKR TWS API."""

import asyncio
import logging
import math
import threading
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ibapi.contract import Contract  # type: ignore[import-untyped]
from ibapi.order import Order as IBOrder  # type: ignore[import-untyped]

from app.broker.ibkr.tws_client import TWSClient
from app.oms.models import OMSOrder, OMSOrderStatus
from app.rms.models import OrderSide

logger = logging.getLogger(__name__)

_MAX_SANE_PRICE = 1e12


def _usable_price(raw: float, fallback: Decimal | None = None) -> Decimal | None:
    """Ignore IBKR UNSET (DBL_MAX) and non-finite prices from orderStatus."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 0.0
    if math.isfinite(value) and 0 < value < _MAX_SANE_PRICE:
        return Decimal(str(value))
    return fallback


class IBKRExecutionAdapter:
    """Execution adapter connecting OMS to IBKR Paper TWS API via TWSClient."""

    def __init__(
        self,
        client: TWSClient | None = None,
        host: str = "127.0.0.1",
        port: int = 7497,
        client_id: int = 1,
        timeout: float = 10.0,
        sec_type: str = "STK",
        exchange: str = "SMART",
        currency: str = "USD",
    ) -> None:
        """Initialize IBKR Execution Adapter with connection config and state tracking."""
        self._client = client or TWSClient()
        self._host = host
        self._port = port
        self._client_id = client_id
        self._timeout = timeout
        self._sec_type = sec_type
        self._exchange = exchange
        self._currency = currency

        self._lock = threading.Lock()

        # Map internal_order_id <-> tws_order_id (int)
        self._orders_by_tws_id: dict[int, OMSOrder] = {}
        self._orders_by_internal_id: dict[str, OMSOrder] = {}
        self._tws_id_to_internal_id: dict[int, str] = {}

        # Futures for async waiting on terminal status / fill per internal order
        self._fill_futures: dict[
            str, tuple[asyncio.Future[OMSOrder], asyncio.AbstractEventLoop]
        ] = {}

        # Register self as TWSClient listener
        self._client.register_listener(self)

    def is_connected(self) -> bool:
        """Return True if connected to TWS and initial handshake completed."""
        return self._client.is_connected()

    async def connect(self) -> None:
        """Establish TCP connection and complete TWS nextValidId handshake."""
        if self.is_connected():
            logger.info("IBKRExecutionAdapter already connected.")
            return

        logger.info(
            "Connecting IBKRExecutionAdapter to TWS Paper at %s:%d (clientID=%d)...",
            self._host,
            self._port,
            self._client_id,
        )
        success = self._client.connect_and_start(
            host=self._host,
            port=self._port,
            client_id=self._client_id,
            timeout=self._timeout,
        )
        if not success:
            raise ConnectionError(
                f"Failed to connect to local Paper TWS at {self._host}:{self._port}."
            )
        logger.info("IBKRExecutionAdapter connected successfully.")

    async def disconnect(self) -> None:
        """Cleanly disconnect from TWS API."""
        logger.info("Disconnecting IBKRExecutionAdapter...")
        self._client.disconnect_clean()

    def _get_next_tws_order_id(self) -> int:
        """Reserve and increment the next valid order ID from TWS under lock."""
        with self._lock:
            current_id = self._client.next_order_id
            if current_id is None:
                current_id = 1
                self._client.next_order_id = 1
            self._client.next_order_id = current_id + 1
            return current_id

    def _build_ibkr_contract(self, symbol: str) -> Contract:
        """Construct standard IBKR Contract model for US Equities / ETFs."""
        contract = Contract()
        contract.symbol = symbol
        contract.secType = self._sec_type
        contract.exchange = self._exchange
        contract.currency = self._currency
        return contract

    def _build_ibkr_order(self, order: OMSOrder) -> IBOrder:
        """Convert internal OMSOrder to IBKR IBOrder model."""
        ib_order = IBOrder()
        ib_order.action = "BUY" if order.side == OrderSide.BUY else "SELL"
        ib_order.totalQuantity = float(order.quantity)

        order_type_upper = (order.order_type or "LIMIT").upper()
        if order_type_upper == "LIMIT":
            if order.limit_price is None:
                raise ValueError("Limit price is required for LIMIT order type.")
            ib_order.orderType = "LMT"
            ib_order.lmtPrice = float(order.limit_price)
        elif order_type_upper == "MARKET":
            ib_order.orderType = "MKT"
        else:
            raise ValueError(f"Unsupported order type: {order.order_type}")

        ib_order.transmit = True
        ib_order.eTradeOnly = False
        ib_order.firmQuoteOnly = False
        if order.intent.ibkr_account:
            ib_order.account = order.intent.ibkr_account
        return ib_order

    async def submit_order(self, order: OMSOrder) -> OMSOrder:
        """Submit internal OMSOrder to IBKR TWS API."""
        if not self.is_connected():
            order.status = OMSOrderStatus.ERROR
            order.error_message = "TWS connection unavailable"
            raise ConnectionError("Cannot submit order: TWS is not connected.")

        with self._lock:
            if order.internal_order_id in self._orders_by_internal_id:
                raise ValueError(
                    f"Duplicate order submission attempt for internal ID: {order.internal_order_id}"
                )

        tws_order_id = self._get_next_tws_order_id()
        order.ibkr_order_id = tws_order_id

        contract = self._build_ibkr_contract(order.symbol)
        ib_order = self._build_ibkr_order(order)

        order.timestamps.ibkr_submit_started_at = datetime.now(UTC)

        with self._lock:
            self._orders_by_tws_id[tws_order_id] = order
            self._orders_by_internal_id[order.internal_order_id] = order
            self._tws_id_to_internal_id[tws_order_id] = order.internal_order_id

        self._client.register_request_id(tws_order_id, "order")

        logger.info(
            "Submitting order to IBKR TWS: internal_id=%s, tws_id=%d, symbol=%s, action=%s, qty=%s, limit_price=%s",
            order.internal_order_id,
            tws_order_id,
            order.symbol,
            ib_order.action,
            order.quantity,
            order.limit_price,
        )

        try:
            # Maps are registered above so openOrder/orderStatus cannot race
            # placeOrder() on a TWS thread (or a synchronous mock callback).
            self._client.placeOrder(tws_order_id, contract, ib_order)
            order.timestamps.ibkr_submit_completed_at = datetime.now(UTC)
            if order.status == OMSOrderStatus.PENDING:
                logger.info(
                    "placeOrder sent; waiting for IBKR confirmation: internal_id=%s tws_id=%d",
                    order.internal_order_id,
                    tws_order_id,
                )
        except Exception as e:
            order.status = OMSOrderStatus.ERROR
            order.error_message = f"Failed to place order with TWS: {e}"
            logger.exception("TWS placeOrder failed for order %s", order.internal_order_id)
            raise

        return order

    def adopt_order(self, order: OMSOrder) -> None:
        """Register an existing OMS order so later broker callbacks can update it."""
        with self._lock:
            self._orders_by_internal_id[order.internal_order_id] = order
            if order.ibkr_order_id is not None:
                tws_id = int(order.ibkr_order_id)
                self._orders_by_tws_id[tws_id] = order
                self._tws_id_to_internal_id[tws_id] = order.internal_order_id

    def fetch_broker_order_snapshot(self) -> bool:
        """Ask TWS for open orders. Returns False if broker state cannot be requested."""
        if not self.is_connected():
            return False
        try:
            req_open = getattr(self._client, "reqOpenOrders", None)
            if callable(req_open):
                req_open()
            req_exec = getattr(self._client, "reqExecutions", None)
            if callable(req_exec):
                from ibapi.execution import ExecutionFilter  # type: ignore[import-untyped]

                req_exec(9003, ExecutionFilter())
            return True
        except Exception:
            logger.exception("Failed to request open orders / executions from TWS")
            return False

    async def cancel_order(self, order_or_id: OMSOrder | str) -> OMSOrder:
        """Cancel open order by OMSOrder instance or internal order ID."""
        internal_order_id = (
            order_or_id.internal_order_id
            if isinstance(order_or_id, OMSOrder)
            else order_or_id
        )
        if not self.is_connected():
            raise ConnectionError("Cannot cancel order: TWS is not connected.")

        with self._lock:
            order = self._orders_by_internal_id.get(internal_order_id)
            if not order:
                raise ValueError(f"Order not found for internal ID: {internal_order_id}")

            if order.status in (
                OMSOrderStatus.FILLED,
                OMSOrderStatus.CANCELLED,
                OMSOrderStatus.REJECTED,
                OMSOrderStatus.ERROR,
            ):
                raise ValueError(f"Cannot cancel order in terminal status: {order.status.value}")

            tws_order_id = int(order.ibkr_order_id) if order.ibkr_order_id else None

        if tws_order_id is None:
            raise ValueError(f"Order {internal_order_id} has no IBKR order ID assigned.")

        logger.info("Canceling IBKR order: internal_id=%s, tws_id=%d", internal_order_id, tws_order_id)
        self._client.cancelOrder(tws_order_id)
        return order

    async def wait_for_terminal_or_fill(
        self, internal_order_id: str, timeout: float = 10.0
    ) -> OMSOrder:
        """Wait asynchronously until order reaches terminal state (FILLED, CANCELLED, REJECTED, ERROR)."""
        loop = asyncio.get_running_loop()

        with self._lock:
            order = self._orders_by_internal_id.get(internal_order_id)
            if not order:
                raise ValueError(f"Order not found: {internal_order_id}")

            if order.status in (
                OMSOrderStatus.FILLED,
                OMSOrderStatus.CANCELLED,
                OMSOrderStatus.REJECTED,
                OMSOrderStatus.ERROR,
            ):
                return order

            future: asyncio.Future[OMSOrder] = loop.create_future()
            self._fill_futures[internal_order_id] = (future, loop)

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            with self._lock:
                self._fill_futures.pop(internal_order_id, None)
                order = self._orders_by_internal_id.get(internal_order_id)
                if order:
                    return order
            raise TimeoutError(f"Timed out waiting for order execution: {internal_order_id}")

    # ── EWrapper Listener Callbacks ───────────────────────────────────

    _TERMINAL_STATUSES = (
        OMSOrderStatus.FILLED,
        OMSOrderStatus.CANCELLED,
        OMSOrderStatus.REJECTED,
        OMSOrderStatus.ERROR,
    )

    def _map_ib_status(self, ib_status: str) -> OMSOrderStatus:
        """Map IBKR orderStatus / orderState.status to internal OMSOrderStatus."""
        status_upper = ib_status.upper().replace(" ", "")
        if status_upper in ("PENDINGSUBMIT", "PRESUBMITTED", "APIPENDING"):
            return OMSOrderStatus.PENDING
        if status_upper in ("SUBMITTED", "PENDINGCANCEL"):
            return OMSOrderStatus.SUBMITTED
        if status_upper in ("PARTIALLYFILLED",):
            return OMSOrderStatus.PARTIALLY_FILLED
        if status_upper in ("FILLED",):
            return OMSOrderStatus.FILLED
        if status_upper in ("CANCELLED", "APICANCELLED"):
            return OMSOrderStatus.CANCELLED
        if status_upper in ("INACTIVE", "REJECTED"):
            return OMSOrderStatus.REJECTED
        return OMSOrderStatus.PENDING

    def _apply_mapped_status(
        self,
        order: OMSOrder,
        mapped_status: OMSOrderStatus,
        *,
        qty_filled: float | None = None,
        qty_remaining: float | None = None,
        now: datetime | None = None,
    ) -> None:
        """Apply broker-mapped status without regressing a terminal order."""
        if order.status in self._TERMINAL_STATUSES:
            return

        if qty_filled is not None:
            order.filled_quantity = max(order.filled_quantity, float(qty_filled))
        if qty_remaining is not None:
            order.remaining_quantity = float(qty_remaining)

        filled = order.filled_quantity
        remaining = order.remaining_quantity

        if mapped_status in (
            OMSOrderStatus.CANCELLED,
            OMSOrderStatus.REJECTED,
            OMSOrderStatus.ERROR,
        ):
            order.status = mapped_status
            return

        if filled > 0 and remaining > 0:
            order.status = OMSOrderStatus.PARTIALLY_FILLED
        elif filled >= order.quantity or mapped_status == OMSOrderStatus.FILLED:
            order.status = OMSOrderStatus.FILLED
            if order.timestamps.execution_received_at is None:
                order.timestamps.execution_received_at = now or datetime.now(UTC)
        else:
            order.status = mapped_status

    def _notify_future_if_terminal(self, order: OMSOrder) -> None:
        """Resolve waiting future if order reached a terminal state."""
        if order.status in (
            OMSOrderStatus.FILLED,
            OMSOrderStatus.CANCELLED,
            OMSOrderStatus.REJECTED,
            OMSOrderStatus.ERROR,
        ):
            fut_tuple = self._fill_futures.pop(order.internal_order_id, None)
            if fut_tuple:
                fut, loop = fut_tuple
                if not fut.done():
                    loop.call_soon_threadsafe(fut.set_result, order)

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
        """Handle orderStatus callback from TWSClient."""
        now = datetime.now(UTC)
        with self._lock:
            order = self._orders_by_tws_id.get(orderId)
            if not order:
                return

            if order.timestamps.order_status_received_at is None:
                order.timestamps.order_status_received_at = now

            mapped_status = self._map_ib_status(status)
            qty_filled = float(filled)
            qty_remaining = float(remaining)

            avg = _usable_price(avgFillPrice, fallback=order.limit_price)
            if avg is not None:
                order.average_fill_price = avg
            last = _usable_price(lastFillPrice, fallback=None)
            if last is not None:
                order.last_fill_price = last

            self._apply_mapped_status(
                order,
                mapped_status,
                qty_filled=qty_filled,
                qty_remaining=qty_remaining,
                now=now,
            )

            logger.info(
                "IBKR orderStatus callback: internal_id=%s, tws_id=%d, raw_status=%s, mapped=%s, filled=%s, rem=%s, avgPrice=%s",
                order.internal_order_id,
                orderId,
                status,
                order.status.value,
                qty_filled,
                qty_remaining,
                avgFillPrice,
            )

            self._notify_future_if_terminal(order)

    def on_open_order(
        self,
        orderId: int,
        contract: Any,
        order: Any,
        orderState: Any,
    ) -> None:
        """Handle openOrder: apply broker orderState.status onto the existing OMSOrder."""
        raw_status = str(getattr(orderState, "status", "") or "")
        if not raw_status:
            return

        now = datetime.now(UTC)
        with self._lock:
            oms_order = self._orders_by_tws_id.get(orderId)
            if not oms_order:
                logger.debug(
                    "Ignoring openOrder for unknown tws_id=%s (no duplicate OMS order created)",
                    orderId,
                )
                return

            if oms_order.timestamps.order_status_received_at is None:
                oms_order.timestamps.order_status_received_at = now

            mapped_status = self._map_ib_status(raw_status)
            self._apply_mapped_status(oms_order, mapped_status, now=now)

            logger.info(
                "IBKR openOrder callback: internal_id=%s, tws_id=%d, raw_status=%s, mapped=%s",
                oms_order.internal_order_id,
                orderId,
                raw_status,
                oms_order.status.value,
            )

            self._notify_future_if_terminal(oms_order)

    def on_exec_details(self, reqId: int, contract: Any, execution: Any) -> None:
        """Handle execDetails fill callback from TWSClient."""
        now = datetime.now(UTC)
        tws_order_id = getattr(execution, "orderId", None)
        if tws_order_id is None:
            tws_order_id = reqId

        with self._lock:
            order = self._orders_by_tws_id.get(tws_order_id)
            if not order:
                return

            order.timestamps.execution_received_at = now

            exec_shares = float(getattr(execution, "shares", 0))
            exec_price = float(getattr(execution, "price", 0.0))
            cum_qty = float(getattr(execution, "cumQty", 0))
            avg_price = float(getattr(execution, "avgPrice", 0.0))

            if cum_qty > 0:
                order.filled_quantity = max(order.filled_quantity, cum_qty)
                order.remaining_quantity = max(0, order.quantity - order.filled_quantity)

            if exec_price > 0:
                order.last_fill_price = Decimal(str(exec_price))
            if avg_price > 0:
                order.average_fill_price = Decimal(str(avg_price))

            if order.filled_quantity >= order.quantity:
                order.status = OMSOrderStatus.FILLED

            logger.info(
                "IBKR execDetails callback: internal_id=%s, exec_shares=%s, exec_price=%.4f, cum_qty=%s, status=%s",
                order.internal_order_id,
                exec_shares,
                exec_price,
                cum_qty,
                order.status.value,
            )

            self._notify_future_if_terminal(order)

    def on_exec_details_end(self, reqId: int) -> None:
        """Handle execDetailsEnd callback from TWSClient."""
        logger.debug("Received execDetailsEnd for reqId=%d", reqId)

    def on_commission_report(self, commissionReport: Any) -> None:
        """Handle commissionReport callback from TWSClient."""
        exec_id = getattr(commissionReport, "execId", None)
        commission = float(getattr(commissionReport, "commission", 0.0))
        currency = getattr(commissionReport, "currency", "")
        logger.info(
            "IBKR commissionReport callback: exec_id=%s, commission=%.2f %s",
            exec_id,
            commission,
            currency,
        )

    def on_error(self, reqId: int, errorCode: int, errorString: str) -> None:
        """Handle TWS error callback."""
        # Check if error corresponds to an active order
        req_type = self._client.get_request_type(reqId)
        with self._lock:
            order = self._orders_by_tws_id.get(reqId) if req_type == "order" or reqId in self._orders_by_tws_id else None

            if order:
                # Code 202 is Order Canceled by user/system
                if errorCode == 202:
                    order.status = OMSOrderStatus.CANCELLED
                    order.error_message = f"Canceled: {errorString}"
                elif errorCode in (201, 10147, 10148, 2109, 200, 399):
                    # Order rejections or failures
                    order.status = OMSOrderStatus.REJECTED
                    order.error_message = f"TWS Error {errorCode}: {errorString}"
                elif (errorCode >= 2000 and errorCode < 3000) or (errorCode >= 10000 and errorCode < 11000):
                    # Informational status notifications or preset warnings (e.g. 10349 TIF set to DAY)
                    logger.info("TWS Status info for order %s: %d %s", order.internal_order_id, errorCode, errorString)
                    return
                else:
                    order.status = OMSOrderStatus.ERROR
                    order.error_message = f"TWS Error {errorCode}: {errorString}"

                logger.warning(
                    "Order %s status updated to %s via TWS error %d: %s",
                    order.internal_order_id,
                    order.status.value,
                    errorCode,
                    errorString,
                )

                self._notify_future_if_terminal(order)

    def on_connection_closed(self) -> None:
        """Handle TWS connection dropped callback."""
        logger.warning("IBKRExecutionAdapter detected connection closed.")
        with self._lock:
            for order in self._orders_by_internal_id.values():
                if order.status not in (
                    OMSOrderStatus.FILLED,
                    OMSOrderStatus.CANCELLED,
                    OMSOrderStatus.REJECTED,
                ):
                    order.status = OMSOrderStatus.ERROR
                    order.error_message = "Connection closed unexpectedly"
                    self._notify_future_if_terminal(order)
