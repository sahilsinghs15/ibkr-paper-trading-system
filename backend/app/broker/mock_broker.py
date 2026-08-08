"""In-memory mock broker for local development and testing.

Implements the full BaseBroker contract without requiring TWS or an IBKR
account.  All state is held in memory.  Orders remain in SUBMITTED state
until explicitly filled via ``simulate_fill()``.
"""

import copy
import logging
import uuid
from decimal import Decimal

from app.broker.base_broker import BaseBroker
from app.models.broker import BrokerStatus, Margin
from app.models.order import Order, OrderSide, OrderStatus
from app.models.position import Position

logger = logging.getLogger(__name__)

# Terminal statuses — orders in these states cannot be modified, cancelled, or filled.
_TERMINAL_STATUSES = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}
)

# Simulated account defaults
_DEFAULT_EQUITY = Decimal(1_000_000)
_DEFAULT_AVAILABLE = Decimal(800_000)
_DEFAULT_BUYING_POWER = Decimal(1_600_000)

# Default fill price for market orders (no market-data dependency yet)
_SIMULATED_MARKET_PRICE = Decimal(100)


class BrokerDisconnectedError(RuntimeError):
    """Raised when a broker operation is attempted without an active session."""


class MockBroker(BaseBroker):
    """Realistic in-memory broker for the paper trading system.

    State model::

        DISCONNECTED ──login()──► CONNECTED
        CONNECTED ──disconnect()──► DISCONNECTED

    Order lifecycle::

        place_order() → PENDING → SUBMITTED
        simulate_fill() → FILLED  (+ position update)
    """

    def __init__(self) -> None:
        self._status: BrokerStatus = BrokerStatus.DISCONNECTED
        self._orders: dict[str, Order] = {}
        self._positions: dict[str, Position] = {}
        self._equity: Decimal = _DEFAULT_EQUITY
        self._available_funds: Decimal = _DEFAULT_AVAILABLE
        self._buying_power: Decimal = _DEFAULT_BUYING_POWER

    # ── helpers ──────────────────────────────────────────────────────

    @property
    def status(self) -> BrokerStatus:
        """Current broker connection status."""
        return self._status

    def _require_connected(self) -> None:
        """Raise if the broker is not connected."""
        if self._status != BrokerStatus.CONNECTED:
            raise BrokerDisconnectedError(
                "Broker is not connected. Call login() first."
            )

    @staticmethod
    def _generate_order_id() -> str:
        return f"MOCK-{uuid.uuid4().hex[:8].upper()}"

    @staticmethod
    def _get_fill_price(order: Order) -> Decimal:
        """Determine the fill price for an order.

        LIMIT orders fill at their specified price.
        MARKET orders fill at a deterministic simulated price.
        """
        if order.price is not None:
            return order.price
        return _SIMULATED_MARKET_PRICE

    def _get_or_create_position(self, symbol: str) -> Position:
        if symbol not in self._positions:
            self._positions[symbol] = Position(
                symbol=symbol,
                quantity=0,
                average_price=Decimal(0),
            )
        return self._positions[symbol]

    def _update_position(self, order: Order) -> None:
        """Update position state after an order fill.

        Supports long and short positions:
        - quantity > 0  → long
        - quantity == 0 → flat
        - quantity < 0  → short

        BUY increases quantity (adds to long / reduces short).
        SELL decreases quantity (reduces long / adds to short).

        When a fill *increases* the absolute position size (same direction),
        the average price is recalculated as a weighted average.
        When a fill *reduces* the position (opposite direction), the average
        price is kept unchanged until the position reaches flat.
        On a reversal through flat the new average price is set to the fill price.
        """
        pos = self._get_or_create_position(order.symbol)
        fill_price = order.average_fill_price or Decimal(0)
        fill_qty = order.filled_quantity

        signed_qty = fill_qty if order.side == OrderSide.BUY else -fill_qty
        new_quantity = pos.quantity + signed_qty

        # Determine whether the fill increases or reduces the position.
        same_direction = (pos.quantity >= 0 and signed_qty > 0) or (
            pos.quantity <= 0 and signed_qty < 0
        )

        if pos.quantity == 0:
            # Opening a new position from flat.
            pos.average_price = fill_price
        elif same_direction:
            # Increasing the position — weighted average.
            total_cost = pos.average_price * abs(pos.quantity) + fill_price * fill_qty
            pos.average_price = total_cost / abs(new_quantity)
        elif new_quantity == 0:
            # Closing to flat — reset average price.
            pos.average_price = Decimal(0)
        elif (pos.quantity > 0 and new_quantity < 0) or (
            pos.quantity < 0 and new_quantity > 0
        ):
            # Reversal through flat — new average is the fill price.
            pos.average_price = fill_price
        # else: partial reduction — average price stays unchanged.

        pos.quantity = new_quantity

        logger.info(
            "Position updated: %s qty=%d avg_price=%s",
            pos.symbol,
            pos.quantity,
            pos.average_price,
        )

    # ── BaseBroker interface ─────────────────────────────────────────

    async def login(self) -> None:
        """Establish the mock broker session."""
        self._status = BrokerStatus.CONNECTED
        logger.info("MockBroker logged in")

    async def disconnect(self) -> None:
        """Close the mock broker session."""
        self._status = BrokerStatus.DISCONNECTED
        logger.info("MockBroker disconnected")

    async def get_positions(self) -> list[Position]:
        """Return copies of all non-flat positions."""
        self._require_connected()
        return [copy.copy(p) for p in self._positions.values() if not p.is_flat]

    async def get_margin(self) -> Margin:
        """Return simulated account margin information."""
        self._require_connected()
        return Margin(
            equity=self._equity,
            available_funds=self._available_funds,
            buying_power=self._buying_power,
        )

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: int,
        order_type: str,
        price: Decimal | None = None,
    ) -> Order:
        """Place an order. The order transitions to SUBMITTED and awaits fill."""
        self._require_connected()

        if not symbol or not symbol.strip():
            raise ValueError("Symbol must be non-empty.")
        if quantity <= 0:
            raise ValueError(f"Quantity must be positive, got {quantity}.")

        order = Order(
            order_id=self._generate_order_id(),
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            price=price,
        )
        order.status = OrderStatus.SUBMITTED
        self._orders[order.order_id] = order

        logger.info(
            "Order submitted: %s %s %s x%d",
            order.order_id,
            order.side.value,
            order.symbol,
            order.quantity,
        )
        return copy.copy(order)

    async def modify_order(
        self,
        order_id: str,
        quantity: int | None = None,
        price: Decimal | None = None,
    ) -> Order:
        """Modify an existing SUBMITTED order."""
        self._require_connected()

        order = self._orders.get(order_id)
        if order is None:
            raise ValueError(f"Unknown order ID: {order_id}")
        if order.status != OrderStatus.SUBMITTED:
            raise ValueError(
                f"Cannot modify order {order_id} in {order.status.value} state."
            )

        if quantity is not None:
            if quantity <= 0:
                raise ValueError(f"Quantity must be positive, got {quantity}.")
            order.quantity = quantity
        if price is not None:
            order.price = price

        logger.info(
            "Order modified: %s qty=%d price=%s",
            order.order_id,
            order.quantity,
            order.price,
        )
        return copy.copy(order)

    async def cancel_order(self, order_id: str) -> Order:
        """Cancel an existing SUBMITTED order."""
        self._require_connected()

        order = self._orders.get(order_id)
        if order is None:
            raise ValueError(f"Unknown order ID: {order_id}")
        if order.status != OrderStatus.SUBMITTED:
            raise ValueError(
                f"Cannot cancel order {order_id} in {order.status.value} state."
            )

        order.status = OrderStatus.CANCELLED
        logger.info("Order cancelled: %s", order.order_id)
        return copy.copy(order)

    async def get_order_book(self) -> list[Order]:
        """Return copies of all orders."""
        self._require_connected()
        return [copy.copy(o) for o in self._orders.values()]

    # ── Test / development helper ────────────────────────────────────

    async def simulate_fill(self, order_id: str) -> Order:
        """Simulate a complete fill for a SUBMITTED order.

        This is a test/development helper — not part of the BaseBroker contract.
        The real IBKRBroker receives fill callbacks from TWS instead.
        """
        self._require_connected()

        order = self._orders.get(order_id)
        if order is None:
            raise ValueError(f"Unknown order ID: {order_id}")
        if order.status != OrderStatus.SUBMITTED:
            raise ValueError(
                f"Cannot fill order {order_id} in {order.status.value} state."
            )

        order.status = OrderStatus.FILLED
        order.filled_quantity = order.quantity
        order.average_fill_price = self._get_fill_price(order)

        logger.info(
            "Order filled: %s price=%s qty=%d",
            order.order_id,
            order.average_fill_price,
            order.filled_quantity,
        )
        self._update_position(order)
        return copy.copy(order)
