"""Order domain model for the paper trading system."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum


class OrderSide(Enum):
    """Side of an order."""

    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(Enum):
    """Lifecycle status of an order."""

    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass
class Order:
    """A trading order in the paper trading system.

    Fields that are unknown at order creation time (fill data) are optional.
    The order is mutable because its status and fill fields update over its lifecycle.
    """

    order_id: str
    symbol: str
    side: OrderSide
    quantity: int
    order_type: str
    status: OrderStatus = OrderStatus.PENDING
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    price: Decimal | None = None
    filled_quantity: int = 0
    average_fill_price: Decimal | None = None
