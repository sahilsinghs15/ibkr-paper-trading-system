"""SQLAlchemy model for CFD symbol master reference table."""

from decimal import Decimal

from sqlalchemy import BigInteger, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class InstrumentModel(Base):
    """CFD symbol master table."""

    __tablename__ = "instruments"

    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    sec_type: Mapped[str] = mapped_column(String, nullable=False)
    trade_conid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    market_data_conid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    underlying_exchange: Mapped[str] = mapped_column(String, nullable=False)
    exchange: Mapped[str] = mapped_column(String, nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False)
    multiplier: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal(1)
    )
    size_increment: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8), nullable=True
    )
