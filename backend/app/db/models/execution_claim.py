"""Durable execution dedupe barrier.

``RMSContext.processed_signals`` is an in-process set written *after* orders
reach the broker, so it cannot stop a replay that crashed mid-execution and it
does not exist at all across processes. This table is the authoritative barrier:
a row is claimed *before* submission and promoted to EXECUTED afterwards, with a
unique constraint doing the enforcement.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

CLAIM_STATE_CLAIMED = "CLAIMED"
CLAIM_STATE_EXECUTED = "EXECUTED"
CLAIM_STATE_ABANDONED = "ABANDONED"


class ExecutionClaimModel(Base):
    """One row per (account, strategy, intent signal_id) execution attempt."""

    __tablename__ = "execution_claims"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dedupe_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    strategy_id: Mapped[str] = mapped_column(String, nullable=False)
    signal_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(
        String, nullable=False, server_default=CLAIM_STATE_CLAIMED
    )
    attempt_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    correlation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_note: Mapped[str | None] = mapped_column(String, nullable=True)
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
