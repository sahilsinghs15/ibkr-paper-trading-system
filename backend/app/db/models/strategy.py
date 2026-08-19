"""SQLAlchemy models for strategies and account allocations."""

from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.account import AccountModel


class StrategyModel(Base):
    """Strategy configuration and position cap table."""

    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    legs: Mapped[int] = mapped_column(Integer, nullable=False)
    expression: Mapped[str] = mapped_column(String, nullable=False, default="CFD")
    max_open_positions: Mapped[int] = mapped_column(Integer, nullable=False)
    weight_source: Mapped[str] = mapped_column(String, nullable=False)
    target_delta: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class AllocationModel(Base):
    """Account-strategy margin allocation and exit parameter table."""

    __tablename__ = "allocations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), nullable=False
    )
    strategy_id: Mapped[str] = mapped_column(
        String, ForeignKey("strategies.strategy_id"), nullable=False
    )
    alloc_pct: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    target: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    stop: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    time_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    max_open_positions: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    account: Mapped["AccountModel"] = relationship("AccountModel")
    strategy: Mapped[StrategyModel] = relationship("StrategyModel")

    __table_args__ = (
        UniqueConstraint(
            "account_id", "strategy_id", name="uq_allocations_account_strategy"
        ),
        CheckConstraint(
            "alloc_pct >= 0 AND alloc_pct <= 1",
            name="ck_allocations_alloc_pct_range",
        ),
    )
