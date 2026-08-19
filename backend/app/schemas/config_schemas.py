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
    allocations: list[AllocationConfigSchema] = Field(default_factory=list)
    symbol_limits: list[SymbolLimitSchema] = Field(default_factory=list)


class AccountsConfigResponse(BaseModel):
    """Top-level GET /config/accounts payload."""

    accounts: list[AccountConfigSchema]


class PatchAccountRequest(BaseModel):
    """Partial update for an account."""

    total_margin: Decimal | None = Field(None, gt=0)
    enabled: bool | None = None


class PatchAllocationRequest(BaseModel):
    """Partial update for an allocation."""

    alloc_pct: Decimal | None = Field(None, ge=0, le=1)
    enabled: bool | None = None
    max_open_positions: int | None = Field(None, ge=0)


class PutSymbolLimitRequest(BaseModel):
    """Upsert per-symbol money limit."""

    money_limit: Decimal = Field(..., gt=0)
