"""SQLAlchemy models for Manual Trading (M0.5 persistence)."""

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.orm.exc import DetachedInstanceError

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.account import AccountModel
    from app.db.models.user import UserModel


class ManualOrderModel(Base):
    """Durable manual order record. Isolated from engine orders (no signal_id)."""

    __tablename__ = "manual_orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), nullable=False, index=True
    )
    ibkr_account: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    internal_order_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    trade_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    broker_order_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    perm_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )

    # Resolved contract details
    con_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    sec_type: Mapped[str] = mapped_column(String(16), nullable=False, default="STK")
    exchange: Mapped[str] = mapped_column(String(32), nullable=False, default="SMART")
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")

    # Order parameters
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # BUY | SELL
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)  # LIMIT | MARKET | STOP
    limit_price: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8), nullable=True
    )
    stop_price: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8), nullable=True
    )
    tif: Mapped[str] = mapped_column(String(16), nullable=False, default="DAY")
    outside_rth: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # Lifecycle status: PENDING_SUBMIT, SUBMITTED, PARTIALLY_FILLED, FILLED, CANCELLED, REJECTED, ERROR
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="PENDING_SUBMIT", index=True
    )
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="manual", server_default="manual"
    )

    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=True
    )
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    account: Mapped["AccountModel"] = relationship("AccountModel")
    user: Mapped["UserModel | None"] = relationship("UserModel")
    executions: Mapped[list["ManualExecutionModel"]] = relationship(
        "ManualExecutionModel", back_populates="manual_order", lazy="selectin"
    )

    @property
    def filled_quantity(self) -> Decimal:
        val = getattr(self, "_filled_quantity", None)
        if val is not None:
            return val
        try:
            if self.executions:
                return sum((e.quantity for e in self.executions), start=Decimal(0))
        except DetachedInstanceError:
            return Decimal(0)
        return Decimal(0)

    @filled_quantity.setter
    def filled_quantity(self, val: Any) -> None:
        self._filled_quantity = Decimal(str(val)) if val is not None else Decimal(0)

    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "idempotency_key",
            name="uq_manual_orders_account_idempotency",
        ),
        Index("ix_manual_orders_account_status", "account_id", "status"),
        Index("ix_manual_orders_account_created_at", "account_id", "created_at"),
    )


class ManualExecutionModel(Base):
    """Execution fills specifically for manual orders. Identity is IBKR exec_id."""

    __tablename__ = "manual_executions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    manual_order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("manual_orders.id"), nullable=False, index=True
    )
    exec_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    commission: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8), nullable=True
    )
    commission_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="manual", server_default="manual"
    )
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    manual_order: Mapped["ManualOrderModel"] = relationship(
        "ManualOrderModel", back_populates="executions"
    )


class ManualPositionModel(Base):
    """Authoritative manual position ledger.

    Multiple manual positions for the same symbol can coexist under different trade_id values.
    """

    __tablename__ = "manual_positions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), nullable=False, index=True
    )
    trade_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    con_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sec_type: Mapped[str] = mapped_column(String(16), nullable=False, default="STK")
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    signed_qty: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal(0)
    )
    avg_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=Decimal(0)
    )
    realized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=Decimal(0)
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="OPEN", index=True
    )  # OPEN | CLOSED
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="manual", server_default="manual"
    )
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    account: Mapped["AccountModel"] = relationship("AccountModel")

    __table_args__ = (
        UniqueConstraint(
            "account_id", "trade_id", name="uq_manual_positions_account_trade"
        ),
        Index("ix_manual_positions_account_status", "account_id", "status"),
        Index("ix_manual_positions_account_symbol", "account_id", "symbol"),
    )


class ManualHaltStateModel(Base):
    """Per-account manual trading halt state (M4 groundwork)."""

    __tablename__ = "manual_halt_state"

    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), primary_key=True
    )
    halted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    halted_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    halted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    account: Mapped["AccountModel"] = relationship("AccountModel")


class ManualAuditEventModel(Base):
    """Tamper-evident audit trail for manual trading actions."""

    __tablename__ = "manual_audit_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), nullable=False, index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    account: Mapped["AccountModel"] = relationship("AccountModel")
    user: Mapped["UserModel | None"] = relationship("UserModel")
