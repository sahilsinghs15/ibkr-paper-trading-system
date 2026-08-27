"""Pydantic schemas for the reconcile positions read-only API."""

from datetime import datetime

from pydantic import BaseModel, Field


class ReconcileRunSummary(BaseModel):
    """Latest position reconcile sweep metadata."""

    id: int | None = Field(None, description="Run row id when a sweep has completed")
    finished_at: datetime | None = Field(None, description="UTC timestamp when the sweep finished")
    timed_out: bool = Field(False, description="True when the last IBKR reqPositions timed out")
    error: str | None = Field(None, description="Sweep error message, if any")
    broker_line_count: int = Field(0, description="Broker lines in the last sweep")
    match_count: int = Field(0, description="Matched symbol nets in the last sweep")
    ghost_count: int = Field(0, description="Ledger-only nets in the last sweep")
    orphan_count: int = Field(0, description="Broker-only nets in the last sweep")
    drift_count: int = Field(0, description="Qty drift nets in the last sweep")
    unmapped_account_count: int = Field(0, description="Broker lines with unknown IBKR account")


class BrokerPositionSnapshotRow(BaseModel):
    """One persisted IBKR broker position line."""

    ibkr_account: str
    con_id: int
    account_id: int | None = None
    symbol: str
    sec_type: str
    currency: str
    exchange: str
    signed_qty: float
    avg_cost: float
    as_of: datetime


class LedgerPositionRow(BaseModel):
    """One OPEN pair row from the Model Blue positions ledger."""

    account_id: int
    ibkr_account: str | None = None
    trade_id: str
    strategy_id: str
    leg_a_symbol: str
    leg_a_signed_qty: float
    leg_a_instrument_type: str
    leg_b_symbol: str | None = None
    leg_b_signed_qty: float | None = None
    leg_b_instrument_type: str | None = None
    risk_state: str


class ReconcileDiffRow(BaseModel):
    """Broker-vs-ledger classification for one symbol net."""

    kind: str
    ibkr_account: str | None = None
    account_id: int | None = None
    symbol: str
    sec_type: str
    con_id: int | None = None
    broker_qty: float | None = None
    ledger_qty: float | None = None
    in_flight: bool = False


class ReconcilePositionsResponse(BaseModel):
    """Read-only reconcile dashboard payload."""

    run: ReconcileRunSummary | None = None
    broker_positions: list[BrokerPositionSnapshotRow] = Field(default_factory=list)
    ledger_positions: list[LedgerPositionRow] = Field(default_factory=list)
    diffs: list[ReconcileDiffRow] = Field(default_factory=list)


class FlattenBrokerPositionRequest(BaseModel):
    """Target one persisted IBKR broker snapshot line for MARKET flatten."""

    ibkr_account: str = Field(..., min_length=1)
    symbol: str = Field(..., min_length=1)
    sec_type: str = Field(..., min_length=1)
    con_id: int = Field(..., gt=0)


class FlattenBrokerPositionResponse(BaseModel):
    """Result of flattening one IBKR broker snapshot line."""

    ibkr_account: str
    account_id: int | None = None
    symbol: str
    sec_type: str
    con_id: int
    side: str
    quantity: float
    status: str
    success: bool
    message: str | None = None
