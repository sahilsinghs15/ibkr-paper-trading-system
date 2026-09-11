"""Pydantic schemas for Manual Trading (M0, M0.5, M1-A contracts)."""

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TradingSource(str, Enum):
    """Authoritative source discriminator for trading entities."""

    MANUAL = "manual"
    ENGINE = "engine"


class ManualOrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class ManualOrderType(str, Enum):
    """Order type for manual orders.

    NOTE: LIMIT and MARKET are candidate order types for M1 execution.
    STOP is supported in persistence schema only and is NOT executable in M1
    unless explicitly approved in a later phase.
    """

    LIMIT = "LIMIT"
    MARKET = "MARKET"
    STOP = "STOP"


class ManualOrderStatus(str, Enum):
    PENDING_SUBMIT = "PENDING_SUBMIT"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    ERROR = "ERROR"


# ── M1-A Gateway Status Schemas ─────────────────────────────────────


class GatewayEnvironmentMode(str, Enum):
    VERIFIED_PAPER = "VERIFIED_PAPER"
    VERIFIED_LIVE = "VERIFIED_LIVE"
    UNKNOWN = "UNKNOWN"


class GatewayModeVerificationState(str, Enum):
    ACCOUNT_PREFIX_VERIFIED = "ACCOUNT_PREFIX_VERIFIED"
    EXPECTED_PAPER_BY_CONFIGURATION = "EXPECTED_PAPER_BY_CONFIGURATION"
    EXPECTED_LIVE_BY_CONFIGURATION = "EXPECTED_LIVE_BY_CONFIGURATION"
    UNVERIFIED = "UNVERIFIED"


class GatewayStatusResponse(BaseModel):
    connected: bool
    host: str
    port: int
    client_id: int
    ibkr_account: str | None = None
    managed_accounts: list[str] = Field(default_factory=list)
    environment: GatewayEnvironmentMode
    verification_state: GatewayModeVerificationState
    server_version: int | None = None
    connection_time: str | None = None
    message: str


# ── M1-A CFD Instrument Discovery Schemas ───────────────────────────


class CfdSearchRequest(BaseModel):
    symbol: str = Field(..., min_length=1, description="Symbol to search for CFD contracts")
    exchange: str = Field(default="SMART", description="Exchange, defaults to SMART")
    currency: str = Field(default="USD", description="Currency, defaults to USD")


class CfdCandidateContract(BaseModel):
    con_id: int
    symbol: str
    sec_type: Literal["CFD"] = "CFD"
    exchange: str
    currency: str
    local_symbol: str | None = None
    trading_class: str | None = None
    min_tick: float | None = None
    primary_exchange: str | None = None
    long_name: str | None = None


class CfdSearchResponse(BaseModel):
    symbol: str
    candidates: list[CfdCandidateContract]
    count: int
    message: str | None = None


# ── M0.5 Persistence Schemas ─────────────────────────────────────────


class ManualOrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    ibkr_account: str
    idempotency_key: str
    internal_order_id: str
    trade_id: str
    broker_order_id: str | None = None
    perm_id: int | None = None
    con_id: int
    symbol: str
    sec_type: str
    exchange: str
    currency: str
    side: str
    quantity: Decimal
    filled_quantity: Decimal = Decimal(0)
    order_type: str
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    tif: str
    outside_rth: bool
    status: str
    source: Literal["manual"] = "manual"
    user_id: int | None = None
    reject_reason: str | None = None
    created_at: datetime
    updated_at: datetime
    submitted_at: datetime | None = None
    completed_at: datetime | None = None


class ManualExecutionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    manual_order_id: int
    exec_id: str
    broker_order_id: str | None = None
    quantity: Decimal
    price: Decimal
    commission: Decimal | None = None
    commission_currency: str | None = None
    source: Literal["manual"] = "manual"
    executed_at: datetime
    created_at: datetime
    updated_at: datetime


class ManualPositionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    trade_id: str
    symbol: str
    con_id: int
    sec_type: str
    currency: str
    signed_qty: Decimal
    avg_cost: Decimal
    realized_pnl: Decimal
    status: str  # OPEN | CLOSED
    source: Literal["manual"] = "manual"
    opened_at: datetime
    closed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ManualPositionsListResponse(BaseModel):
    account_id: int
    ibkr_account: str
    source: Literal["manual"] = "manual"
    positions: list[ManualPositionRead]
    total: int


class ManualOrdersListResponse(BaseModel):
    account_id: int
    ibkr_account: str
    source: Literal["manual"] = "manual"
    orders: list[ManualOrderRead]
    total: int


class ManualHaltStateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_id: int
    halted: bool
    halted_by: str | None = None
    halted_at: datetime | None = None
    reason: str | None = None


class ManualAuditEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    user_id: int | None = None
    action: str
    request_id: str | None = None
    payload: dict[str, Any]
    created_at: datetime


# ── M1-B Manual Order Preview & Submission Schemas ───────────────────


class ManualOrderPreviewRequest(BaseModel):
    symbol: str = Field(..., min_length=1)
    con_id: int = Field(..., gt=0, description="Resolved IBKR contract conId")
    sec_type: Literal["CFD"] = "CFD"
    exchange: str = Field(default="SMART")
    currency: str = Field(default="USD")
    side: Literal["BUY", "SELL"]
    quantity: Decimal = Field(..., gt=Decimal(0))
    order_type: Literal["LIMIT", "MARKET", "STOP"]
    limit_price: Decimal | None = None
    tif: str = Field(default="DAY")
    outside_rth: bool = Field(default=False)
    min_tick: float | None = None
    trade_id: str | None = None


class ManualOrderPreviewResponse(BaseModel):
    valid: bool
    symbol: str
    con_id: int
    sec_type: str
    exchange: str
    currency: str
    side: str
    quantity: Decimal
    order_type: str
    limit_price: Decimal | None = None
    effective_price: Decimal | None = None
    notional: Decimal | None = None
    notional_status: str  # "EXACT" | "NO_MARKET_PRICE"
    init_margin_change: Decimal | None = None
    maint_margin_change: Decimal | None = None
    margin_status: str  # "AVAILABLE" | "UNAVAILABLE" | "TIMEOUT" | "SKIPPED"
    gateway_connected: bool
    environment: GatewayEnvironmentMode
    account_enabled: bool
    kill_switch_active: bool
    trading_paused: bool
    manual_halted: bool
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class ManualOrderSubmitRequest(BaseModel):
    idempotency_key: str = Field(..., min_length=1, max_length=128)
    symbol: str = Field(..., min_length=1)
    con_id: int = Field(..., gt=0)
    sec_type: Literal["CFD"] = "CFD"
    exchange: str = Field(default="SMART")
    currency: str = Field(default="USD")
    side: Literal["BUY", "SELL"]
    quantity: Decimal = Field(..., gt=Decimal(0))
    order_type: Literal["LIMIT", "MARKET", "STOP"]
    limit_price: Decimal | None = None
    tif: str = Field(default="DAY")
    outside_rth: bool = Field(default=False)
    min_tick: float | None = None
    trade_id: str | None = None


class ManualOrderSubmitResponse(BaseModel):
    order: ManualOrderRead
    idempotent_replay: bool = False
    message: str


class ManualOrderCancelResponse(BaseModel):
    order: ManualOrderRead
    success: bool
    status: str  # "CANCELLED" | "CANCELLED_LOCALLY" | "CANCEL_REQUESTED" | "ALREADY_CANCELLED"
    message: str
    broker_order_id: int | None = None
    perm_id: int | None = None

