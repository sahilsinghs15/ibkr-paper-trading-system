"""SQLAlchemy model for multi-leg basket execution state."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.account import AccountModel


class BasketModel(Base):
    """Account-scoped multi-leg basket. Not a position ledger."""

    __tablename__ = "baskets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), nullable=False
    )
    trade_id: Mapped[str] = mapped_column(String, nullable=False)
    strategy_id: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    intended_leg_count: Mapped[int] = mapped_column(Integer, nullable=False)
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
            "account_id", "trade_id", "action", name="uq_baskets_account_trade_action"
        ),
    )
