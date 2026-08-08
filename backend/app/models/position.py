"""Position domain model representing a current holding."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class Position:
    """A trading position for a single instrument.

    Mutable because position state updates as orders fill.
    """

    symbol: str
    quantity: int
    average_price: Decimal
    unrealized_pnl: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)

    @property
    def is_flat(self) -> bool:
        """A position is flat when there are no shares held."""
        return self.quantity == 0
