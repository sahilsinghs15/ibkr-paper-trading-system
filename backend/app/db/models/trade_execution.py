"""Durable Trade Book execution model - IBKR source of truth, idempotent on exec_id."""

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
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


class TradeExecutionModel(Base):
    """One broker execution fill - Trade Book. Identity is IBKR execId."""

    __tablename__ = "trade_executions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    exec_id: Mapped[str] = mapped_column(String, nullable=False)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"), nullable=False, index=True)
    ibkr_account: Mapped[str] = mapped_column(String, nullable=False)
    order_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("orders.id"), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    sec_type: Mapped[str] = mapped_column(String, nullable=False)
    exchange: Mapped[str] = mapped_column(String, nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False, server_default="USD")
    con_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    side: Mapped[str] = mapped_column(String, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    cum_qty: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False, server_default="0")
    avg_price: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False, server_default="0")
    broker_order_id: Mapped[str | None] = mapped_column(String, nullable=True)
    perm_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    client_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    commission: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    commission_currency: Mapped[str | None] = mapped_column(String, nullable=True)
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    correction_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    account: Mapped["AccountModel"] = relationship("AccountModel")
    order: Mapped["OrderModel | None"] = relationship("OrderModel")

    __table_args__ = (
        UniqueConstraint("exec_id", name="uq_trade_executions_exec_id"),
        Index("ix_trade_executions_account_executed_at", "account_id", "executed_at"),
        Index("ix_trade_executions_account_symbol", "account_id", "symbol"),
        Index("ix_trade_executions_broker_order_id", "broker_order_id"),
    )
