"""Abstract base broker interface.

Defines the contract that all broker implementations (MockBroker, IBKRBroker)
must satisfy.  The interface is asynchronous to align with the asyncio/FastAPI
application model and the event-driven nature of broker communication.
"""

from abc import ABC, abstractmethod
from decimal import Decimal

from app.models.broker import Margin
from app.models.order import Order, OrderSide
from app.models.position import Position


class BaseBroker(ABC):
    """Abstract broker interface for the paper trading system.

    Subclasses must implement every abstract method.  The interface uses
    domain models for inputs and return types — no raw dictionaries.
    """

    @abstractmethod
    async def login(self) -> None:
        """Establish and validate a broker session."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Gracefully close the broker connection."""

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Return all current positions."""

    @abstractmethod
    async def get_margin(self) -> Margin:
        """Return current account margin information."""

    @abstractmethod
    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: int,
        order_type: str,
        price: Decimal | None = None,
    ) -> Order:
        """Submit a new order and return the resulting Order object."""

    @abstractmethod
    async def modify_order(
        self,
        order_id: str,
        quantity: int | None = None,
        price: Decimal | None = None,
    ) -> Order:
        """Modify an existing open order."""

    @abstractmethod
    async def cancel_order(self, order_id: str) -> Order:
        """Cancel an existing open order."""

    @abstractmethod
    async def get_order_book(self) -> list[Order]:
        """Return the current list of orders."""
