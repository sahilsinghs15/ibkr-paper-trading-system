"""Snapshot of ledger positions at account-flatten initiation.

Captures the exact set of engine/manual positions that existed when the
account kill-switch was initiated, so reconciliation operates against that
immutable set and does not accidentally include positions opened after the
flatten began (concurrency requirement §9).
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class KillSwitchFlattenSnapshotModel(Base):
    """One row per ledger position captured at flatten initiation."""

    __tablename__ = "kill_switch_flatten_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    operation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("kill_switch_operations.operation_id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"), nullable=False, index=True)
    position_type: Mapped[str] = mapped_column(String(16), nullable=False)  # 'engine' | 'manual'
    trade_id: Mapped[str] = mapped_column(String, nullable=False, index=True)

    # Engine-specific (nullable for manual)
    leg_a_symbol: Mapped[str | None] = mapped_column(String, nullable=True)
    leg_a_signed_qty: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    leg_a_entry_mark: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    leg_a_instrument_type: Mapped[str | None] = mapped_column(String, nullable=True)
    leg_b_symbol: Mapped[str | None] = mapped_column(String, nullable=True)
    leg_b_signed_qty: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    leg_b_entry_mark: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    leg_b_instrument_type: Mapped[str | None] = mapped_column(String, nullable=True)

    # Manual-specific (nullable for engine)
    manual_position_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    symbol: Mapped[str | None] = mapped_column(String, nullable=True)
    con_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sec_type: Mapped[str | None] = mapped_column(String, nullable=True)
    side: Mapped[str | None] = mapped_column(String(8), nullable=True)
    signed_qty: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    avg_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)

    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
