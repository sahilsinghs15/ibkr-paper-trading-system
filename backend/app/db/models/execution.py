"""SQLAlchemy model for IBKR execution/fill ledger."""

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.account import AccountModel
    from app.db.models.order import OrderModel


class ExecutionModel(Base):
    """One IBKR execution. Identity is broker execId (or a synthetic fallback)."""

    __tablename__ = "executions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    exec_id: Mapped[str] = mapped_column(String, nullable=False)
    order_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("orders.id"), nullable=True, index=True
    )
    account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), nullable=True
    )
    internal_order_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    broker_order_id: Mapped[str | None] = mapped_column(String, nullable=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    commission: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    commission_currency: Mapped[str | None] = mapped_column(String, nullable=True)
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    perm_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    order: Mapped["OrderModel | None"] = relationship("OrderModel")
    account: Mapped["AccountModel | None"] = relationship("AccountModel")

    __table_args__ = (
        UniqueConstraint("exec_id", name="uq_executions_exec_id"),
    )
