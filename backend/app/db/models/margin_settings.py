"""Singleton operator-tunable margin-gate policy."""

from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MarginSettingsModel(Base):
    """One-row dashboard config for RMS check 1 and the pre-sizing margin gate."""

    __tablename__ = "margin_settings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    check_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    gate_basis: Mapped[str] = mapped_column(String, nullable=False, default="available_funds")
    min_free_buffer: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal(0)
    )
    min_free_pct_of_netliq: Mapped[Decimal] = mapped_column(
        Numeric(9, 6), nullable=False, default=Decimal("0.050000")
    )
    comfort_ratio: Mapped[Decimal] = mapped_column(
        Numeric(9, 6), nullable=False, default=Decimal("0.800000")
    )
    confirm_borderline: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    enforce_look_ahead: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reject_on_stale_snapshot: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    default_rate: Mapped[Decimal] = mapped_column(
        Numeric(9, 6), nullable=False, default=Decimal("0.300000")
    )
    rate_safety_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(9, 6), nullable=False, default=Decimal("1.100000")
    )

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_margin_settings_singleton"),
        CheckConstraint(
            "gate_basis IN ('available_funds', 'excess_liquidity')",
            name="ck_margin_settings_gate_basis",
        ),
        CheckConstraint("min_free_buffer >= 0", name="ck_margin_settings_min_free_buffer"),
        CheckConstraint(
            "min_free_pct_of_netliq >= 0 AND min_free_pct_of_netliq <= 1",
            name="ck_margin_settings_min_free_pct",
        ),
        CheckConstraint(
            "comfort_ratio > 0 AND comfort_ratio <= 1",
            name="ck_margin_settings_comfort_ratio",
        ),
        CheckConstraint(
            "default_rate > 0 AND default_rate <= 1",
            name="ck_margin_settings_default_rate",
        ),
        CheckConstraint(
            "rate_safety_multiplier >= 1 AND rate_safety_multiplier <= 2",
            name="ck_margin_settings_rate_safety",
        ),
    )
