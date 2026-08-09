"""API request and response schemas for the paper trading system."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.order import OrderSide, OrderStatus
from app.models.signal import SignalType


class MarketDataEventRequest(BaseModel):
    """Request schema for submitting a market data event."""

    timestamp: datetime = Field(
        ...,
        description="Timezone-aware timestamp of the price update.",
        examples=["2025-06-15T10:00:00Z"],
    )
    price: Decimal = Field(
        ...,
        description="Observed asset price.",
        gt=0,
        examples=["105.50"],
    )
    volume: int = Field(
        ...,
        description="Trading volume associated with this update.",
        ge=0,
        examples=[100],
    )


class SignalSchema(BaseModel):
    """Response schema representing a trading signal."""

    model_config = ConfigDict(from_attributes=True)

    signal_type: SignalType = Field(..., description="The generated signal action.")
    timestamp: datetime = Field(..., description="When the signal was generated.")
    reason: str = Field(..., description="Explanation for the signal generation.")


class OrderSchema(BaseModel):
    """Response schema representing a broker order."""

    model_config = ConfigDict(from_attributes=True)

    order_id: str = Field(..., description="Unique order identifier.")
    symbol: str = Field(..., description="Asset symbol.")
    side: OrderSide = Field(..., description="BUY or SELL side.")
    quantity: int = Field(..., description="Order quantity.")
    order_type: str = Field(..., description="Order execution type.")
    status: OrderStatus = Field(..., description="Current status of the order.")
    timestamp: datetime = Field(..., description="Order placement timestamp.")
    price: Decimal | None = Field(None, description="Limit price if applicable.")
    filled_quantity: int = Field(0, description="Quantity filled so far.")
    average_fill_price: Decimal | None = Field(None, description="Average fill price.")


class MarketDataResponse(BaseModel):
    """Response schema for a market data event submission."""

    candle_completed: bool = Field(
        ..., description="Whether this event completed a candle."
    )
    signal: SignalSchema | None = Field(
        None,
        description="The resulting signal if a candle completed.",
    )
    order: OrderSchema | None = Field(
        None,
        description="The resulting order if a trade was triggered.",
    )


class PositionSchema(BaseModel):
    """Response schema representing an active trading position."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str = Field(..., description="Symbol of the position.")
    quantity: int = Field(..., description="Number of units held.")
    average_price: Decimal = Field(..., description="Average cost price.")
    unrealized_pnl: Decimal = Field(Decimal(0), description="Unrealized P&L.")
    realized_pnl: Decimal = Field(Decimal(0), description="Realized P&L.")


class MarginSchema(BaseModel):
    """Response schema representing account margin details."""

    model_config = ConfigDict(from_attributes=True)

    equity: Decimal = Field(..., description="Total account equity.")
    available_funds: Decimal = Field(..., description="Funds available for trading.")
    buying_power: Decimal = Field(..., description="Available buying power.")
