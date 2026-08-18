"""SQLAlchemy model for pair-level position ledger."""

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.account import AccountModel


class PositionModel(Base):
    """Pair-level position ledger table."""

    __tablename__ = "positions"

    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), primary_key=True
    )
    trade_id: Mapped[str] = mapped_column(String, primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String, nullable=False)
    leg_a_symbol: Mapped[str] = mapped_column(String, nullable=False)
    leg_a_signed_qty: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    leg_a_entry_mark: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    leg_b_symbol: Mapped[str | None] = mapped_column(String, nullable=True)
    leg_b_signed_qty: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    leg_b_entry_mark: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    realised_pnl: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=Decimal(0)
    )
    commission: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=Decimal(0)
    )
    live_pnl: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=Decimal(0)
    )
    target: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    stop: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    time_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    leg_a_instrument_type: Mapped[str] = mapped_column(String, nullable=False, default="STK")
    leg_b_instrument_type: Mapped[str | None] = mapped_column(String, nullable=True)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    risk_state: Mapped[str] = mapped_column(String, nullable=False)

    account: Mapped["AccountModel"] = relationship("AccountModel")
