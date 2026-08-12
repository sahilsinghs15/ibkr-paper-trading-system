"""TradingView Webhook response schemas."""

from pydantic import BaseModel, Field


class TradingViewWebhookResponse(BaseModel):
    """Response schema for TradingView webhook ingestion."""

    status: str = Field("received", description="Ingestion acknowledgment status.")
    source: str = Field("tradingview", description="Source identifier.")
