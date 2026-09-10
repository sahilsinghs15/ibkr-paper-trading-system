"""Per-account loss threshold crossing state."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Numeric, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AccountLossStateModel(Base):
    __tablename__ = "account_loss_state"

    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"), primary_key=True)
    is_below: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    last_threshold: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
