"""Singleton paper execution / auto square-off settings."""

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ExecutionSettingsModel(Base):
    """One-row dashboard config for basket fill timeout and incomplete-leg retries."""

    __tablename__ = "execution_settings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    square_off_after_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    retry_interval_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    retry_window_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=30)

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_execution_settings_singleton"),
        CheckConstraint("square_off_after_sec > 0", name="ck_execution_settings_timeout_pos"),
        CheckConstraint("max_retries >= 0", name="ck_execution_settings_retries_nonneg"),
        CheckConstraint("retry_interval_sec > 0", name="ck_execution_settings_interval_pos"),
        CheckConstraint("retry_window_sec > 0", name="ck_execution_settings_window_pos"),
        CheckConstraint(
            "retry_window_sec >= retry_interval_sec",
            name="ck_execution_settings_window_ge_interval",
        ),
    )
