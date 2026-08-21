"""TradingView Webhook response schemas."""

from pydantic import BaseModel, Field


class TradingViewWebhookResponse(BaseModel):
    """Response schema for TradingView webhook ingestion."""

    status: str = Field("accepted", description="Ingestion acknowledgment status.")
    source: str = Field("tradingview", description="Source identifier.")
    signal_id: str | None = Field(None, description="Ingested signal ID if available.")
    job_id: str | None = Field(None, description="Durable job ID if available.")
    request_id: str | None = Field(None, description="Trace request correlation ID.")
