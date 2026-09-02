"""SQLAlchemy models for accounts and per-symbol limits."""

from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class AccountModel(Base):
    """Account-level configuration and margin control table."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    ibkr_account: Mapped[str] = mapped_column(String, nullable=False)
    total_margin: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    default_symbol_limit: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 4), nullable=True, default=None
    )

    __table_args__ = (
        CheckConstraint("total_margin > 0", name="ck_accounts_total_margin_positive"),
    )


class PerSymbolLimitModel(Base):
    """Per-account per-symbol exposure limit table."""

    __tablename__ = "per_symbol_limits"

    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), primary_key=True
    )
    money_limit: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)

    account: Mapped[AccountModel] = relationship("AccountModel")
