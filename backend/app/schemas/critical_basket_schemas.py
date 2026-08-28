"""Pydantic schemas for critical basket incidents."""

from datetime import datetime

from pydantic import BaseModel, Field


class CriticalBasketLegRow(BaseModel):
    """One non-compensation leg from a CRITICAL basket."""

    leg: str
    symbol: str
    sec_type: str
    con_id: int | None = None
    intended_qty: float
    filled_qty: float
    status: str


class CriticalBasketRow(BaseModel):
    """One CRITICAL (or recovering) basket incident."""

    basket_id: int
    account_id: int
    ibkr_account: str
    strategy_id: str
    trade_id: str
    action: str
    state: str
    recovery_status: str | None = None
    recovery_detail: str | None = None
    recovered_at: datetime | None = None
    intended_leg_count: int
    legs: list[CriticalBasketLegRow] = Field(default_factory=list)
    updated_at: datetime | None = None


class CriticalBasketsResponse(BaseModel):
    """List of active critical basket incidents for one IBKR account."""

    ibkr_account: str
    incidents: list[CriticalBasketRow] = Field(default_factory=list)
