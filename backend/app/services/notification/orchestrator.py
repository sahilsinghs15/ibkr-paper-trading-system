"""Central Notification Orchestrator managing event admission, deduplication, and durable persistence."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.db.models.event import EventLogModel
from app.db.models.notification import NotificationDeliveryModel, NotificationLogModel
from app.services.notification.intelligence import NotificationIntelligenceEngine
from app.services.notification.normalizer import EventNormalizer
from app.services.notification.types import (
    ChannelType,
    DeliveryStatus,
    NormalizedEvent,
    NotificationStatus,
)

logger = logging.getLogger(__name__)


class NotificationOrchestrator:
    """Orchestrates notification policy, deduplication, intelligence, and outbox persistence."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        default_telegram_chat_id: str | None = None,
        default_sms_recipient: str | None = None,
        default_whatsapp_recipient: str | None = None,
        default_max_retries: int | None = None,
        intelligence_engine: NotificationIntelligenceEngine | None = None,
        shadow_mode: bool | None = None,
    ) -> None:
        self._session_factory = session_factory
        settings = get_settings()
        self._default_telegram_chat_id = (
            default_telegram_chat_id or settings.telegram_chat_id or ""
        )
        self._default_sms_recipient = (
            default_sms_recipient or settings.sms_recipient_phone or ""
        )
        self._default_whatsapp_recipient = (
            default_whatsapp_recipient or settings.whatsapp_recipient_phone or ""
        )
        # Per-channel default recipients, keyed by ChannelType value
        self._channel_defaults: dict[str, str] = {
            ChannelType.TELEGRAM.value: self._default_telegram_chat_id,
            ChannelType.SMS.value: self._default_sms_recipient,
            ChannelType.WHATSAPP.value: self._default_whatsapp_recipient,
        }
        self._default_max_retries = (
            default_max_retries
            if default_max_retries is not None
            else settings.notification_max_retries
        )
        self._intelligence = intelligence_engine or NotificationIntelligenceEngine()
        self._shadow_mode = (
            shadow_mode if shadow_mode is not None else settings.notification_shadow_mode
        )

    def determine_channels(
        self,
        event: NormalizedEvent,
        requested_channels: list[ChannelType | str] | None = None,
    ) -> list[str]:
        """Determine target channel names for this event under current routing policy.

        Routing policy (configurable via settings):
          - Telegram: always active (all severities)
          - SMS:       active when event severity meets notification_sms_min_severity threshold
          - WhatsApp:  active when event severity meets notification_whatsapp_min_severity threshold

        Callers may override by passing an explicit requested_channels list.
        Future channels are addable by registering an adapter and adjusting thresholds —
        no changes to EventNormalizer, IntelligenceEngine, or DB schema required.
        """
        if requested_channels is not None:
            return [
                c.value if isinstance(c, ChannelType) else c.upper()
                for c in requested_channels
            ]

        settings = get_settings()
        channels: list[str] = [ChannelType.TELEGRAM.value]

        # Severity ordering for threshold comparison
        _severity_order: dict[str, int] = {
            "INFO": 0,
            "WARNING": 1,
            "CRITICAL": 2,
        }
        event_sev = _severity_order.get(event.severity.value.upper(), 0)

        sms_threshold = _severity_order.get(
            settings.notification_sms_min_severity.upper(), 2
        )
        if settings.sms_enabled and event_sev >= sms_threshold:
            channels.append(ChannelType.SMS.value)

        wa_threshold = _severity_order.get(
            settings.notification_whatsapp_min_severity.upper(), 2
        )
        if settings.whatsapp_enabled and event_sev >= wa_threshold:
            channels.append(ChannelType.WHATSAPP.value)

        return channels

    async def ingest_event(
        self,
        event_input: NormalizedEvent | dict[str, Any],
        *,
        channels: list[ChannelType | str] | None = None,
        recipient_overrides: dict[str, str] | None = None,
        max_retries: int | None = None,
        fail_silent: bool = True,
    ) -> NotificationLogModel | None:
        """Admit an authoritative event, apply deduplication, and persist outbox records.

        Never performs external network calls.
        Safe for execution-critical callers when fail_silent=True.
        """
        recipient_overrides = recipient_overrides or {}
        effective_retries = (
            max_retries if max_retries is not None else self._default_max_retries
        )
        try:
            event = EventNormalizer.normalize(event_input)
            target_channels = self.determine_channels(event, channels)

            acc = str(event.details.get("account_id") or event.details.get("ibkr_account") or "global")
            sym = str(event.details.get("symbol") or "global")
            scope_key = f"{event.category}:{acc}:{sym}"

            async with self._session_factory() as session, session.begin():
                # Advisory lock for PostgreSQL to serialize concurrent admissions on the same event scope
                try:
                    dialect = getattr(getattr(session, "bind", None), "dialect", None)
                    if getattr(dialect, "name", "") == "postgresql":
                        lock_key = f"{event.event_type}:{event.correlation_id or scope_key}"
                        await session.execute(
                            sa.text("SELECT pg_advisory_xact_lock(hashtext(:k)::bigint)"),
                            {"k": lock_key},
                        )
                except (SQLAlchemyError, OSError) as lock_err:
                    logger.debug("Advisory lock skipped: %s", lock_err)

                # Deduplication check at logical notification level
                if event.dedupe_key:
                    stmt = select(NotificationLogModel).where(
                        NotificationLogModel.dedupe_key == event.dedupe_key
                    )
                    existing = (await session.execute(stmt)).scalar_one_or_none()
                    if existing is not None:
                        logger.info(
                            "Deduplicated event '%s' matching existing notification %s (dedupe_key=%s)",
                            event.event_type,
                            existing.notification_id,
                            event.dedupe_key,
                        )
                        return existing

                # Intelligence admission evaluation (cooldown, rate limit, flapping, recovery correlation)
                decision = await self._intelligence.evaluate_admission(session, event, scope_key)

                notif_id = f"NOTIF-{uuid.uuid4().hex[:16].upper()}"

                if not decision.admit:
                    suppressed_values = {
                        "notification_id": notif_id,
                        "source_event_id": event.source_event_id,
                        "event_type": event.event_type,
                        "category": event.category,
                        "severity": event.severity.value,
                        "title": event.title,
                        "message": event.message,
                        "payload": event.details,
                        "dedupe_key": event.dedupe_key,
                        "correlation_id": event.correlation_id,
                        "suppressed_reason": decision.suppressed_reason,
                        "status": NotificationStatus.SUPPRESSED.value,
                        "created_at": datetime.now(UTC),
                    }
                    stmt_sup = (
                        insert(NotificationLogModel)
                        .values(**suppressed_values)
                        .returning(NotificationLogModel.id)
                    )
                    pk_sup = (await session.execute(stmt_sup)).scalar_one()
                    fetch_sup = select(NotificationLogModel).where(NotificationLogModel.id == pk_sup)
                    return (await session.execute(fetch_sup)).scalar_one()

                title = event.title
                message = event.message
                if decision.is_flapping and decision.flapping_alert_needed:
                    title = f"⚠️ Subsystem {event.category} is FLAPPING"
                    message = f"Rapid state oscillations detected for {scope_key}. Notifications throttled until stabilized."

                notif_values = {
                    "notification_id": notif_id,
                    "source_event_id": event.source_event_id,
                    "event_type": event.event_type,
                    "category": event.category,
                    "severity": event.severity.value,
                    "title": title,
                    "message": message,
                    "payload": event.details,
                    "dedupe_key": event.dedupe_key,
                    "correlation_id": event.correlation_id,
                    "status": NotificationStatus.PENDING.value,
                    "created_at": datetime.now(UTC),
                }

                # Insert logical notification record
                if event.dedupe_key:
                    insert_notif_stmt = (
                        insert(NotificationLogModel)
                        .values(**notif_values)
                        .on_conflict_do_nothing(index_elements=["dedupe_key"])
                        .returning(NotificationLogModel.id)
                    )
                    notif_res = await session.execute(insert_notif_stmt)
                    notif_pk = notif_res.scalar_one_or_none()
                    if notif_pk is None:
                        # Another concurrent worker inserted this dedupe_key just now
                        stmt = select(NotificationLogModel).where(
                            NotificationLogModel.dedupe_key == event.dedupe_key
                        )
                        existing = (await session.execute(stmt)).scalar_one()
                        return existing
                else:
                    insert_notif_stmt = (
                        insert(NotificationLogModel)
                        .values(**notif_values)
                        .returning(NotificationLogModel.id)
                    )
                    notif_res = await session.execute(insert_notif_stmt)
                    notif_pk = notif_res.scalar_one()

                # Insert channel deliveries for each target channel
                for ch in target_channels:
                    delivery_id = f"DELIV-{uuid.uuid4().hex[:16].upper()}"
                    recipient = recipient_overrides.get(ch)
                    if not recipient:
                        # Look up per-channel default recipient; fall back to generic placeholder
                        recipient = self._channel_defaults.get(ch) or "default"

                    # Provider name is derived generically per channel;
                    # the adapter implementation owns the actual provider semantics.
                    provider = f"{ch.lower()}_provider"

                    deliv_status = (
                        DeliveryStatus.DELIVERED.value
                        if self._shadow_mode
                        else DeliveryStatus.PENDING.value
                    )
                    provider_msg_id = "SHADOW_DELIVERED" if self._shadow_mode else None
                    delivered_at = datetime.now(UTC) if self._shadow_mode else None

                    deliv_values = {
                        "delivery_id": delivery_id,
                        "notification_id": notif_pk,
                        "channel": ch,
                        "provider": provider,
                        "recipient": recipient,
                        "status": deliv_status,
                        "attempt_count": 0,
                        "max_retries": effective_retries,
                        "provider_message_id": provider_msg_id,
                        "delivered_at": delivered_at,
                    }

                    insert_deliv_stmt = (
                        insert(NotificationDeliveryModel)
                        .values(**deliv_values)
                        .on_conflict_do_nothing(
                            constraint="uq_notification_delivery_channel"
                        )
                    )
                    await session.execute(insert_deliv_stmt)

                # Mirror admitted event to event_log so frontend Notification Center and Telegram stay 100% in real-time sync.
                # Producers that write their own row (the reconciler writes ROGUE_*
                # with its own idempotency key) opt out, otherwise the feed shows
                # and counts the same event twice.
                if not getattr(event, "mirror_to_event_log", True):
                    logger.debug(
                        "Skipped event_log mirror for %s; producer owns its own row",
                        event.event_type,
                    )
                else:
                    try:
                        event_row = EventLogModel(
                            process=str(event.category or "system").lower(),
                            kind=event.event_type,
                            detail={
                                **(event.details or {}),
                                "title": title,
                                "message": message,
                                "severity": event.severity.value,
                                "notification_id": notif_id,
                            },
                        )
                        session.add(event_row)
                    except Exception as mirror_err:  # noqa: BLE001
                        logger.warning("Failed mirroring notification to event_log: %s", mirror_err)

                # Fetch full persisted model for caller
                fetch_stmt = (
                    select(NotificationLogModel)
                    .where(NotificationLogModel.id == notif_pk)
                )
                logical_record = (await session.execute(fetch_stmt)).scalar_one()
                logger.info(
                    "Persisted logical notification: id=%s title='%s' severity=%s channels=%s",
                    notif_id,
                    event.title,
                    event.severity.value,
                    target_channels,
                )
                return logical_record

        except Exception:
            logger.exception("NotificationOrchestrator failed to ingest event")
            if not fail_silent:
                raise
            return None
