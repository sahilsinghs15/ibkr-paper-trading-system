"""Core domain types, enums, and data transfer models for the notification subsystem."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class NotificationSeverity(str, Enum):
    """Notification urgency and priority level."""

    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class NotificationStatus(str, Enum):
    """Status of the logical notification."""

    PENDING = "PENDING"
    DISPATCHED = "DISPATCHED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SUPPRESSED = "SUPPRESSED"


class DeliveryStatus(str, Enum):
    """Delivery lifecycle status for an individual channel delivery."""

    PENDING = "PENDING"
    SENDING = "SENDING"
    DELIVERED = "DELIVERED"
    RETRYING = "RETRYING"
    FAILED = "FAILED"
    ABANDONED = "ABANDONED"


class ChannelType(str, Enum):
    """Notification delivery channel identity."""

    TELEGRAM = "TELEGRAM"
    # Future channel types reserved for Phase 4 / future extensions
    SMS = "SMS"
    WHATSAPP = "WHATSAPP"
    EMAIL = "EMAIL"
    SLACK = "SLACK"


@dataclass
class NormalizedEvent:
    """Normalized internal representation of an authoritative OEMS domain event."""

    event_type: str
    title: str
    message: str
    category: str = "SYSTEM"
    severity: NotificationSeverity = NotificationSeverity.INFO
    source: str = "oems"
    source_event_id: str | None = None
    dedupe_key: str | None = None
    correlation_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class DeliveryResult:
    """Outcome of a channel adapter dispatch attempt."""

    success: bool
    provider_message_id: str | None = None
    error_message: str | None = None
    is_retryable: bool = False
    error_details: dict[str, Any] | None = None
