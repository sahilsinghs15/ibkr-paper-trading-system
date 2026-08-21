"""SQLAlchemy model for durable Kill Switch Emergency Flatten operations."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

KILL_SWITCH_STATUS_IDLE = "IDLE"
KILL_SWITCH_STATUS_ACTIVATING = "ACTIVATING"
KILL_SWITCH_STATUS_FLATTENING = "FLATTENING"
KILL_SWITCH_STATUS_RECONCILING = "RECONCILING"
KILL_SWITCH_STATUS_RETRYING = "RETRYING"
KILL_SWITCH_STATUS_FLAT = "FLAT"
KILL_SWITCH_STATUS_COMPLETE = "COMPLETE"
KILL_SWITCH_STATUS_UNRESOLVED = "UNRESOLVED"


class KillSwitchOperationModel(Base):
    """SQLAlchemy model tracking durable emergency flatten / kill switch execution operations."""

    __tablename__ = "kill_switch_operations"

    operation_id: Mapped[Any] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid4, server_default=func.gen_random_uuid()
    )
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"), nullable=False, index=True)
    ibkr_account: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default=KILL_SWITCH_STATUS_ACTIVATING, index=True
    )
    requested_by: Mapped[str] = mapped_column(String, nullable=False, default="operator")
    initial_position_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    flattened_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    working_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    retrying_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    unresolved_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    final_exposure: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
