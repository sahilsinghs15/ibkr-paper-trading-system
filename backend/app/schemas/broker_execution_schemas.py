"""Pydantic schemas for the broker executions Trade Book API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class BrokerExecutionLineSchema(BaseModel):
    """One IBKR Gateway execution line."""

    exec_id: str = Field(..., description="Unique IBKR execution ID")
    executed_at: str = Field(..., description="Execution timestamp (ISO 8601 string)")
    ibkr_account: str = Field(..., description="IBKR account code")
    symbol: str = Field(..., description="Contract symbol")
    sec_type: str = Field(..., description="Security type (e.g. STK, CFD)")
    currency: str = Field(..., description="Currency code (e.g. USD)")
    exchange: str = Field(..., description="Exchange name")
    con_id: int = Field(..., description="IBKR contract ID")
    side: str = Field(..., description="Order side: BUY or SELL")
    quantity: float = Field(..., description="Executed fill quantity")
    price: float = Field(..., description="Executed fill price")
    cum_qty: float = Field(..., description="Cumulative fill quantity for the order")
    avg_price: float = Field(..., description="Average fill price for the order")
    broker_order_id: int | None = Field(None, description="IBKR order ID when known")
    perm_id: int | None = Field(None, description="IBKR permanent order ID")
    client_id: int | None = Field(None, description="IBKR client ID")
    commission: float | None = Field(None, description="Commission amount")
    commission_currency: str | None = Field(None, description="Commission currency")
    realized_pnl: float | None = Field(None, description="Realized PnL from execution")


class BrokerExecutionsResponse(BaseModel):
    """Response payload for GET /api/v1/broker/executions."""

    ibkr_account: str = Field(..., description="Queried IBKR account code")
    as_of: datetime = Field(..., description="Timestamp when the snapshot was taken")
    timed_out: bool = Field(False, description="True if the Gateway execution request timed out")
    window: str = Field("since_midnight", description="Execution window, fixed to since_midnight")
    executions: list[BrokerExecutionLineSchema] = Field(
        default_factory=list, description="List of broker executions"
    )
