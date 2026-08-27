"""SQLAlchemy models for IBKR broker position snapshots and reconcile runs."""

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.account import AccountModel


class BrokerPositionModel(Base):
    """Latest IBKR position inventory line (full replace each snapshot)."""

    __tablename__ = "broker_positions"

    ibkr_account: Mapped[str] = mapped_column(String, primary_key=True)
    con_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), nullable=True
    )
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    sec_type: Mapped[str] = mapped_column(String, nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False)
    exchange: Mapped[str] = mapped_column(String, nullable=False, default="")
    signed_qty: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    avg_cost: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    as_of: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    account: Mapped["AccountModel | None"] = relationship("AccountModel")

    __table_args__ = (
        UniqueConstraint("ibkr_account", "con_id", name="uq_broker_positions_account_conid"),
    )


class PositionReconcileRunModel(Base):
    """One row per periodic broker-vs-ledger reconcile sweep."""

    __tablename__ = "position_reconcile_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    timed_out: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    broker_line_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    match_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    ghost_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    orphan_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    drift_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    unmapped_account_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    mismatches: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
