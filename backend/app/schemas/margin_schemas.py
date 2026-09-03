"""Read schemas for live IBKR account-margin snapshots."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class AccountMarginResponse(BaseModel):
    """One account's live margin snapshot plus tally-adjusted headroom."""

    model_config = ConfigDict(from_attributes=True)

    ibkr_account: str
    currency: str | None = None
    as_of: datetime | None = None
    is_stale: bool
    gate_basis: str
    net_liquidation: Decimal | None = None
    available_funds: Decimal | None = None
    excess_liquidity: Decimal | None = None
    full_init_margin_req: Decimal | None = None
    full_maint_margin_req: Decimal | None = None
    buying_power: Decimal | None = None
    gross_position_value: Decimal | None = None
    total_cash_value: Decimal | None = None
    cushion: Decimal | None = None
    look_ahead_init_margin_req: Decimal | None = None
    look_ahead_maint_margin_req: Decimal | None = None
    look_ahead_available_funds: Decimal | None = None
    look_ahead_excess_liquidity: Decimal | None = None
    look_ahead_next_change: datetime | None = None
    free_margin: Decimal | None = None
    effective_free_margin: Decimal | None = None
    pending_commitments: Decimal
    floor: Decimal
    utilisation_pct: Decimal | None = None


class AccountMarginListResponse(BaseModel):
    accounts: list[AccountMarginResponse]
