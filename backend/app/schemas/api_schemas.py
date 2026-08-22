"""API request and response schemas for the paper trading execution system."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.oms.models import OMSOrderStatus as OrderStatus
from app.rms.models import OrderSide


class OrderSchema(BaseModel):
    """Response schema representing a broker order."""

    model_config = ConfigDict(from_attributes=True)

    order_id: str = Field(..., description="Unique order identifier.")
    symbol: str = Field(..., description="Asset symbol.")
    side: OrderSide = Field(..., description="BUY or SELL side.")
    quantity: float = Field(..., description="Order quantity.")
    order_type: str = Field(..., description="Order execution type.")
    status: OrderStatus = Field(..., description="Current status of the order.")
    timestamp: datetime = Field(..., description="Order placement timestamp.")
    price: Decimal | None = Field(None, description="Limit price if applicable.")
    filled_quantity: float = Field(0, description="Quantity filled so far.")
    average_fill_price: Decimal | None = Field(None, description="Average fill price.")
