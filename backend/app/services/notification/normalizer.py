"""Event normalizer converting authoritative OEMS inputs into stable NormalizedEvents."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.services.notification.types import NormalizedEvent, NotificationSeverity

logger = logging.getLogger(__name__)


class EventNormalizer:
    """Normalizes heterogeneous domain events into canonical NormalizedEvent instances."""

    @classmethod
    def normalize(
        cls,
        event: NormalizedEvent | dict[str, Any],
        *,
        default_category: str = "SYSTEM",
        default_severity: NotificationSeverity = NotificationSeverity.INFO,
    ) -> NormalizedEvent:
        """Convert input event representation into a validated NormalizedEvent."""
        if isinstance(event, NormalizedEvent):
            return event

        if not isinstance(event, dict):
            raise TypeError(f"Expected NormalizedEvent or dict, got {type(event).__name__}")

        # Extract event identity and type
        event_type = (
            event.get("event_type")
            or event.get("kind")
            or event.get("type")
            or "UNKNOWN_EVENT"
        )

        # Extract title and message
        title = event.get("title")
        message = event.get("message")

        # Extract severity with graceful fallback
        raw_severity = event.get("severity") or event.get("level")
        severity = default_severity
        if raw_severity:
            try:
                severity = NotificationSeverity(str(raw_severity).upper())
            except ValueError:
                logger.debug(
                    "Unrecognized severity '%s' for event '%s', defaulting to %s",
                    raw_severity,
                    event_type,
                    default_severity.value,
                )
                severity = default_severity

        # Extract category
        category = str(event.get("category") or default_category).upper()

        # Extract details/payload
        details = event.get("details") or event.get("detail") or {}
        if not isinstance(details, dict):
            details = {"raw": details}

        # Fallbacks for title and message if not explicitly supplied
        if not title:
            title = event.get("friendly_name") or event_type.replace("_", " ").title()
        if not message:
            message = details.get("message") or title

        source_event_id = event.get("source_event_id") or event.get("event_id")
        if source_event_id is not None:
            source_event_id = str(source_event_id)

        dedupe_key = event.get("dedupe_key") or event.get("idempotency_key")
        if dedupe_key is not None:
            dedupe_key = str(dedupe_key)

        correlation_id = event.get("correlation_id")
        if correlation_id is not None:
            correlation_id = str(correlation_id)

        source = str(event.get("source") or "oems")

        timestamp = event.get("timestamp") or event.get("ts")
        if not isinstance(timestamp, datetime):
            timestamp = datetime.now(UTC)
        elif timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)

        return NormalizedEvent(
            event_type=str(event_type),
            category=category,
            severity=severity,
            title=str(title),
            message=str(message),
            source=source,
            source_event_id=source_event_id,
            dedupe_key=dedupe_key,
            correlation_id=correlation_id,
            details=dict(details),
            timestamp=timestamp,
        )
