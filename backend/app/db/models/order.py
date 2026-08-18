"""SQLAlchemy model for order ledger and handoff."""

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.account import AccountModel
    from app.db.models.signal import SignalModel


class OrderModel(Base):
    """Order ledger and handoff table."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("signals.id"), nullable=False, index=True
    )
    trade_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    internal_order_id: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    basket_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("baskets.id"), nullable=True
    )
    is_compensation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    compensation_of_internal_order_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), nullable=False
    )
    strategy_id: Mapped[str] = mapped_column(String, nullable=False)
    leg: Mapped[str] = mapped_column(String, nullable=False)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    ibkr_contract: Mapped[str] = mapped_column(String, nullable=False)
    buy_sell: Mapped[str] = mapped_column(String, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    limit_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    broker_order_id: Mapped[str | None] = mapped_column(String, nullable=True)
    fill_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    fill_qty: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    filled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    margin_impact: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    signal: Mapped["SignalModel"] = relationship("SignalModel")
    account: Mapped["AccountModel"] = relationship("AccountModel")

    __table_args__ = (
        Index("ix_orders_account_status", "account_id", "status"),
    )
