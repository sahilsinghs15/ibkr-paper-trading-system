"""Domain types for an open Model Blue pair trade (persistence-agnostic)."""

from dataclasses import dataclass
from decimal import Decimal

from app.rms.models import OrderSide


@dataclass(frozen=True)
class OpenModelBlueTradeLeg:
    """One open Model Blue leg stored for later CLOSE flattening."""

    symbol: str
    instrument_type: str
    side: OrderSide
    quantity: Decimal
    price: Decimal


@dataclass(frozen=True)
class OpenModelBlueTrade:
    """Record of a sized Model Blue pair that is currently open."""

    trade_id: str
    strategy_id: str
    direction: int
    legs: tuple[OpenModelBlueTradeLeg, ...]
