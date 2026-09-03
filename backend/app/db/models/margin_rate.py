"""Directional per-instrument IBKR margin rates measured by what-if probes."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MarginRateModel(Base):
    """Measured init-margin rate for one (symbol, instrument type, side)."""

    __tablename__ = "margin_rates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    instrument_type: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    probe_notional: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    init_margin_change: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    scanned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "instrument_type",
            "side",
            name="uq_margin_rates_symbol_type_side",
        ),
        CheckConstraint("rate > 0 AND rate <= 1", name="ck_margin_rates_rate_range"),
        CheckConstraint("side IN ('BUY', 'SELL')", name="ck_margin_rates_side"),
    )
