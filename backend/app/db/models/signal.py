"""SQLAlchemy model for TradingView/external signals."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, DateTime, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SignalModel(Base):
    """TradingView / External signal inbox table."""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(String, nullable=False)
    signal_id: Mapped[str] = mapped_column(String, nullable=False)
    trade_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String, nullable=False)
    pair: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)
    ref_price_a: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    ref_price_b: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    reject_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("strategy_id", "signal_id", name="uq_signals_strategy_signal"),
    )


JOB_STATUS_RECEIVED = "RECEIVED"
JOB_STATUS_QUEUED = "QUEUED"
JOB_STATUS_CLAIMED = "CLAIMED"
JOB_STATUS_PROCESSING = "PROCESSING"
JOB_STATUS_COMPLETED = "COMPLETED"
JOB_STATUS_REJECTED = "REJECTED"
JOB_STATUS_FAILED = "FAILED"
JOB_STATUS_RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
JOB_STATUS_DEAD_LETTER = "DEAD_LETTER"

# Statuses where a worker holds a live lease on the job. Every lease predicate
# (claim, heartbeat, reclaim) MUST use this tuple -- if PROCESSING is omitted
# from any one of them the lease silently stops being maintained the moment
# real execution work begins.
ACTIVE_LEASE_STATUSES = (JOB_STATUS_CLAIMED, JOB_STATUS_PROCESSING)

# Statuses a worker may pick up from the queue.
CLAIMABLE_STATUSES = (JOB_STATUS_QUEUED, JOB_STATUS_RECEIVED)


class SignalJobModel(Base):
    """SQLAlchemy model for durable signal execution jobs."""

    __tablename__ = "signal_jobs"

    job_id: Mapped[Any] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    signal_id: Mapped[str] = mapped_column(String, nullable=False)
    strategy_id: Mapped[str] = mapped_column(String, nullable=False)
    trade_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=JOB_STATUS_RECEIVED)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    account_scope: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    capture_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    correlation_id: Mapped[str] = mapped_column(String, nullable=False)
    attempt_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="3")
    worker_id: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

