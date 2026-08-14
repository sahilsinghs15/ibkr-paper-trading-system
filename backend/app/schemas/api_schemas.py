"""API request and response schemas for the paper trading execution system."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.signal import SignalType
from app.oms.models import OMSOrderStatus as OrderStatus
from app.rms.models import OrderSide


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


class PlaceOrderRequest(BaseModel):
    """Request schema for placing a new order."""

    symbol: str = Field(
        ...,
        description="Asset symbol to trade.",
        examples=["AAPL"],
    )
    side: OrderSide = Field(..., description="BUY or SELL side.")
    quantity: int = Field(
        ...,
        description="Number of units to trade.",
        gt=0,
        examples=[1],
    )
    order_type: str = Field(
        ...,
        description="Order execution type: MARKET or LIMIT.",
        examples=["LIMIT"],
    )
    price: Decimal | None = Field(
        None,
        description="Limit price (required for LIMIT orders).",
        examples=["150.00"],
    )


class ModifyOrderRequest(BaseModel):
    """Request schema for modifying an existing order."""

    quantity: int | None = Field(
        None,
        description="New quantity (must be positive).",
        gt=0,
    )
    price: Decimal | None = Field(
        None,
        description="New limit price.",
    )


class BrokerStatusResponse(BaseModel):
    """Response schema representing broker connection status."""

    broker_mode: str = Field(..., description="Active broker environment (ibkr).")
    connected: bool = Field(..., description="Whether the broker is connected.")
    broker_type: str = Field(
        ..., description="Concrete broker class name, e.g. IBKRBroker."
    )
