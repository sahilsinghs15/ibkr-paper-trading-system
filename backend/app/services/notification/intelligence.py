"""Notification intelligence engine: cooldowns, rate limits, flapping, and incident correlation."""

from __future__ import annotations

import logging
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models.notification import NotificationDeliveryModel, NotificationLogModel
from app.services.notification.types import (
    DeliveryStatus,
    NormalizedEvent,
    NotificationSeverity,
    NotificationStatus,
)

logger = logging.getLogger(__name__)


@dataclass
class AdmissionDecision:
    """Decision made by the intelligence engine on whether to dispatch an event."""

    admit: bool
    suppressed_reason: str | None = None
    is_flapping: bool = False
    flapping_alert_needed: bool = False
    incident_notification_id: str | None = None


class NotificationIntelligenceEngine:
    """Evaluates cooldowns, hourly rate limits, flapping suppression, and incident correlation."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # In-memory sliding window for flapping detection: scope -> deque of timestamps
        self._flapping_history: dict[str, deque[float]] = defaultdict(deque)
        self._flapping_alerted: dict[str, bool] = defaultdict(bool)

    def get_cooldown_seconds(self, severity: NotificationSeverity, event_type: str) -> float:
        """Resolve cooldown duration based on severity and event type."""
        if event_type in ("STARTUP_AGGREGATION", "BROKER_RECONNECTED"):
            return 0.0
        settings = get_settings()
        if severity == NotificationSeverity.CRITICAL:
            return settings.notification_cooldown_critical_sec
        if severity == NotificationSeverity.WARNING:
            return settings.notification_cooldown_warning_sec
        return settings.notification_cooldown_info_sec

    def get_hourly_limit(self, severity: NotificationSeverity) -> int:
        """Resolve max hourly notification volume per severity."""
        settings = get_settings()
        if severity == NotificationSeverity.CRITICAL:
            return settings.notification_hourly_limit_critical
        if severity == NotificationSeverity.WARNING:
            return settings.notification_hourly_limit_warning
        return settings.notification_hourly_limit_info

    def evaluate_flapping(self, scope_key: str) -> tuple[bool, bool]:
        """Detect rapid state oscillations.

        Returns (is_flapping, flapping_alert_needed).
        """
        settings = get_settings()
        window_sec = settings.notification_flapping_window_sec
        threshold = settings.notification_flapping_threshold
        now_ts = datetime.now(UTC).timestamp()

        with self._lock:
            history = self._flapping_history[scope_key]
            # Evict timestamps outside the sliding window
            cutoff = now_ts - window_sec
            while history and history[0] < cutoff:
                history.popleft()

            history.append(now_ts)

            if len(history) >= threshold:
                alert_needed = not self._flapping_alerted[scope_key]
                self._flapping_alerted[scope_key] = True
                return True, alert_needed

            # Reset flapping alerted flag if oscillations cooled down below threshold
            if self._flapping_alerted[scope_key] and len(history) < threshold:
                self._flapping_alerted[scope_key] = False

            return False, False

    async def evaluate_admission(
        self,
        session: AsyncSession,
        event: NormalizedEvent,
        scope_key: str,
    ) -> AdmissionDecision:
        """Evaluate event against cooldown, volume limits, flapping, and recovery correlation."""
        now = datetime.now(UTC)
        is_recovery = any(
            k in event.event_type.upper()
            for k in ("RECOVER", "RESOLV", "RECONNECT", "RESTORE")
        ) or event.event_type == "SERVICE_STARTED"

        # 1. Incident / Recovery Correlation check
        if is_recovery:
            # Query for the incident that this recovery resolves
            # Match by correlation_id or category where severity was warning/critical
            incident_query = (
                select(NotificationLogModel)
                .where(
                    NotificationLogModel.status != NotificationStatus.SUPPRESSED.value,
                    (
                        (NotificationLogModel.correlation_id == event.correlation_id)
                        if event.correlation_id
                        else (NotificationLogModel.category == event.category)
                    ),
                    NotificationLogModel.severity.in_(
                        [NotificationSeverity.CRITICAL.value, NotificationSeverity.WARNING.value]
                    ),
                    NotificationLogModel.created_at < now,
                )
                .order_by(NotificationLogModel.id.desc())
                .limit(1)
            )
            prior_incident = (await session.execute(incident_query)).scalar_one_or_none()

            # Rule: If the incident was never alerted (or never existed), suppress recovery
            if prior_incident is None:
                logger.info(
                    "Recovery event '%s' suppressed: no prior alerted incident found for correlation_id=%s",
                    event.event_type,
                    event.correlation_id,
                )
                return AdmissionDecision(
                    admit=False,
                    suppressed_reason="UNALERTED_INCIDENT",
                )

            # Check if there was already a subsequent recovery resolving this incident
            subsequent_recovery = (
                await session.execute(
                    select(NotificationLogModel)
                    .where(
                        NotificationLogModel.status != NotificationStatus.SUPPRESSED.value,
                        (
                            (NotificationLogModel.correlation_id == event.correlation_id)
                            if event.correlation_id
                            else (NotificationLogModel.category == event.category)
                        ),
                        NotificationLogModel.id > prior_incident.id,
                        NotificationLogModel.created_at < now,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if subsequent_recovery is not None:
                logger.info(
                    "Recovery event '%s' suppressed: incident %s already resolved by %s",
                    event.event_type,
                    prior_incident.notification_id,
                    subsequent_recovery.notification_id,
                )
                return AdmissionDecision(
                    admit=False,
                    suppressed_reason="UNALERTED_INCIDENT",
                )

            # Check if prior incident deliveries actually succeeded (or were dispatched)
            deliv_query = select(NotificationDeliveryModel.status).where(
                NotificationDeliveryModel.notification_id == prior_incident.id
            )
            deliv_statuses = (await session.execute(deliv_query)).scalars().all()
            was_alerted = any(
                s in (DeliveryStatus.DELIVERED.value, DeliveryStatus.SENDING.value, DeliveryStatus.PENDING.value)
                for s in deliv_statuses
            )
            if not was_alerted and deliv_statuses:
                logger.info(
                    "Recovery event '%s' suppressed: prior incident %s was never delivered to operator",
                    event.event_type,
                    prior_incident.notification_id,
                )
                return AdmissionDecision(
                    admit=False,
                    suppressed_reason="UNALERTED_INCIDENT",
                )

            # Legitimate recovery from an alerted incident
            return AdmissionDecision(
                admit=True,
                incident_notification_id=prior_incident.notification_id,
            )

        # 2. Flapping Detection
        is_flapping, alert_needed = self.evaluate_flapping(scope_key)
        if is_flapping and not alert_needed:
            logger.warning(
                "Event '%s' suppressed due to active flapping on scope '%s'",
                event.event_type,
                scope_key,
            )
            return AdmissionDecision(
                admit=False,
                suppressed_reason="FLAPPING",
                is_flapping=True,
                flapping_alert_needed=False,
            )

        # 3. Cooldown Anti-Spam Check (bypassed only for the first flapping warning)
        if not (is_flapping and alert_needed):
            cooldown_sec = self.get_cooldown_seconds(event.severity, event.event_type)
            if cooldown_sec > 0:
                cutoff = now - timedelta(seconds=cooldown_sec)
                filters = [
                    NotificationLogModel.event_type == event.event_type,
                    NotificationLogModel.status != NotificationStatus.SUPPRESSED.value,
                    NotificationLogModel.created_at >= cutoff,
                ]
                if event.correlation_id:
                    filters.append(NotificationLogModel.correlation_id == event.correlation_id)
                recent_stmt = (
                    select(NotificationLogModel)
                    .where(*filters)
                    .order_by(NotificationLogModel.id.desc())
                    .limit(1)
                )
                recent_notif = (await session.execute(recent_stmt)).scalar_one_or_none()
                if recent_notif is not None:
                    # Check if there was a recovery event following recent_notif
                    # If the prior incident was already resolved, this is a genuine new incident, not duplicate spam
                    recovery_query = (
                        select(NotificationLogModel.id)
                        .where(
                            NotificationLogModel.status != NotificationStatus.SUPPRESSED.value,
                            (
                                (NotificationLogModel.correlation_id == event.correlation_id)
                                if event.correlation_id
                                else (NotificationLogModel.category == event.category)
                            ),
                            NotificationLogModel.id > recent_notif.id,
                            NotificationLogModel.created_at <= now,
                        )
                        .limit(1)
                    )
                    has_recovered = (await session.execute(recovery_query)).scalar_one_or_none() is not None
                    if not has_recovered:
                        logger.info(
                            "Event '%s' suppressed: within %.1fs cooldown of notification %s",
                            event.event_type,
                            cooldown_sec,
                            recent_notif.notification_id,
                        )
                        return AdmissionDecision(
                            admit=False,
                            suppressed_reason="COOLDOWN",
                        )

        # 4. Hourly Rate / Volume Limit Check (exempting critical and startup aggregation milestones)
        if event.event_type != "STARTUP_AGGREGATION" and event.severity != NotificationSeverity.CRITICAL:
            hourly_limit = self.get_hourly_limit(event.severity)
            if hourly_limit > 0:
                hour_ago = now - timedelta(seconds=3600)
                count_stmt = (
                    select(func.count())
                    .select_from(NotificationLogModel)
                    .where(
                        NotificationLogModel.severity == event.severity.value,
                        NotificationLogModel.status != NotificationStatus.SUPPRESSED.value,
                        NotificationLogModel.created_at >= hour_ago,
                    )
                )
                volume_last_hour = (await session.execute(count_stmt)).scalar_one()
                if volume_last_hour >= hourly_limit:
                    logger.warning(
                        "Event '%s' suppressed: hourly limit of %d reached for severity %s",
                        event.event_type,
                        hourly_limit,
                        event.severity.value,
                    )
                    return AdmissionDecision(
                        admit=False,
                        suppressed_reason="HOURLY_LIMIT",
                    )

        return AdmissionDecision(
            admit=True,
            is_flapping=is_flapping,
            flapping_alert_needed=alert_needed,
        )
