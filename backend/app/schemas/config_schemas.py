"""Pydantic schemas for dashboard config CRUD."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class SymbolLimitSchema(BaseModel):
    """Per-account per-symbol money limit."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    money_limit: Decimal


class AllocationConfigSchema(BaseModel):
    """Account-strategy allocation row for the settings UI."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    strategy_id: str
    alloc_pct: Decimal
    enabled: bool
    max_open_positions: int
    target: Decimal
    stop: Decimal
    time_limit: int


class AccountConfigSchema(BaseModel):
    """Account with nested allocations and symbol limits."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    ibkr_account: str
    total_margin: Decimal
    enabled: bool
    default_symbol_limit: Decimal | None = Decimal("10000000.00")
    kill_switch_active: bool = False
    allocations: list[AllocationConfigSchema] = Field(default_factory=list)
    symbol_limits: list[SymbolLimitSchema] = Field(default_factory=list)


class AccountsConfigResponse(BaseModel):
    """Top-level GET /config/accounts payload."""

    accounts: list[AccountConfigSchema]


class CreateAccountRequest(BaseModel):
    """Payload to create a new paper trading account."""

    name: str = Field(..., min_length=1)
    ibkr_account: str = Field(..., min_length=1)
    total_margin: Decimal = Field(..., gt=0)
    enabled: bool = True
    default_symbol_limit: Decimal | None = Field(None, gt=0)


class PatchAccountRequest(BaseModel):
    """Partial update for an account."""

    name: str | None = Field(None, min_length=1)
    ibkr_account: str | None = Field(None, min_length=1)
    total_margin: Decimal | None = Field(None, gt=0)
    enabled: bool | None = None
    default_symbol_limit: Decimal | None = Field(None, gt=0)


class CreateAllocationRequest(BaseModel):
    """Payload to assign a strategy allocation to an account."""

    strategy_id: str = Field(..., min_length=1)
    alloc_pct: Decimal = Field(..., ge=0, le=1)
    max_open_positions: int | None = Field(None, ge=0)
    target: Decimal = Field(Decimal("500.00"), gt=0)
    stop: Decimal = Field(Decimal("250.00"), gt=0)
    time_limit: int = Field(3600, gt=0)
    enabled: bool = True


class AccountDeleteCheckResponse(BaseModel):
    """Response indicating whether an account can be deleted."""

    can_delete: bool
    reason: str | None = None
    has_history: bool = False


class PatchAllocationRequest(BaseModel):
    """Partial update for an allocation."""

    alloc_pct: Decimal | None = Field(None, ge=0, le=1)
    enabled: bool | None = None
    max_open_positions: int | None = Field(None, ge=0)


class PutSymbolLimitRequest(BaseModel):
    """Upsert per-symbol money limit."""

    money_limit: Decimal = Field(..., gt=0)


class PutDefaultSymbolLimitRequest(BaseModel):
    """Update account default symbol money limit."""

    default_symbol_limit: Decimal = Field(..., gt=0)


class ExecutionSettingsSchema(BaseModel):
    """Paper auto square-off and incomplete-leg retry settings."""

    model_config = ConfigDict(from_attributes=True)

    enabled: bool
    square_off_after_sec: int
    max_retries: int
    retry_interval_sec: int
    retry_window_sec: int
    paper_retries_active: bool = False


class PatchExecutionSettingsRequest(BaseModel):
    """Partial update for execution retry settings."""

    enabled: bool | None = None
    square_off_after_sec: int | None = Field(None, gt=0)
    max_retries: int | None = Field(None, ge=0)
    retry_interval_sec: int | None = Field(None, gt=0)
    retry_window_sec: int | None = Field(None, gt=0)


class SquareOffResponse(BaseModel):
    """Response payload for account-scoped emergency Kill Switch."""

    account_id: int
    ibkr_account: str
    squared_off_count: int
    trade_ids: list[str] = Field(default_factory=list)
    operation_id: str | None = None
    status: str | None = None


class KillSwitchClearResponse(BaseModel):
    """Response payload for clearing an account kill switch."""

    account_id: int
    ibkr_account: str
    operations_cleared: int
    kill_switch_active: bool


class KillSwitchStatusResponse(BaseModel):
    """Response payload reporting kill switch arm state for an account."""

    account_id: int
    kill_switch_active: bool


class ClosePairResponse(BaseModel):
    """Response payload for closing a single selected open position/pair."""

    account_id: int
    ibkr_account: str
    trade_id: str
    leg_a_symbol: str
    leg_b_symbol: str | None = None
    status: str
    success: bool
    message: str | None = None

