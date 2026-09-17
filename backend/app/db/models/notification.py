"""SQLAlchemy ORM models for centralized notification log and channel delivery state."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class NotificationLogModel(Base):
    """Authoritative durable record of a logical operator notification."""

    __tablename__ = "notification_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    notification_id: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    source_event_id: Mapped[str | None] = mapped_column(
        String(128), index=True, nullable=True
    )
    event_type: Mapped[str] = mapped_column(
        String(64), nullable=False, default="UNKNOWN", index=True
    )
    category: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )  # SYSTEM, BROKER, RISK, RECONCILE, etc.
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, index=True
    )  # INFO, WARNING, CRITICAL
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    dedupe_key: Mapped[str | None] = mapped_column(
        String(128), unique=True, index=True, nullable=True
    )
    correlation_id: Mapped[str | None] = mapped_column(
        String(128), index=True, nullable=True
    )
    suppressed_reason: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )  # COOLDOWN, FLAPPING, HOURLY_LIMIT, UNALERTED_INCIDENT
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="PENDING", index=True
    )  # PENDING, DISPATCHED, COMPLETED, FAILED, SUPPRESSED
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    deliveries: Mapped[list[NotificationDeliveryModel]] = relationship(
        "NotificationDeliveryModel",
        back_populates="notification",
        cascade="all, delete-orphan",
    )


class NotificationDeliveryModel(Base):
    """Channel-specific delivery tracking record for a logical notification."""

    __tablename__ = "notification_deliveries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    delivery_id: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    notification_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("notification_log.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )  # TELEGRAM, SMS (future), WHATSAPP (future)
    provider: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # telegram_bot, etc.
    recipient: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="PENDING", index=True
    )  # PENDING, SENDING, DELIVERED, RETRYING, FAILED, ABANDONED
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    provider_message_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    error_details: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    notification: Mapped[NotificationLogModel] = relationship(
        "NotificationLogModel",
        back_populates="deliveries",
    )

    __table_args__ = (
        UniqueConstraint(
            "notification_id",
            "channel",
            name="uq_notification_delivery_channel",
        ),
    )
