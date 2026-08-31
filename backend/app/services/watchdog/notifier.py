"""Notification queue, deduplication, and formatting — spec-compliant, priority-aware."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import UTC, datetime

from app.services.watchdog.config import WatchdogSettings
from app.services.watchdog.models import (
    HealthResult,
    NotificationEvent,
    ServiceName,
    ServiceSnapshot,
)
from app.services.watchdog.telegram import TelegramClient

logger = logging.getLogger(__name__)

# Severity classification per spec
_CRITICAL_EVENTS = {
    NotificationEvent.FAILURE,
    NotificationEvent.RECOVERY_FAILED,
    NotificationEvent.MANUAL_INTERVENTION_REQUIRED,
    NotificationEvent.TRADING_BLOCKED,
}
_WARNING_EVENTS = {
    NotificationEvent.UNHEALTHY,
    NotificationEvent.RECOVERY_STARTED,
}
_INFO_EVENTS = {
    NotificationEvent.START,
    NotificationEvent.STOP,
    NotificationEvent.RECOVERED,
}

def _severity(event: NotificationEvent) -> tuple[str, str]:
    if event in _CRITICAL_EVENTS:
        return "🚨", "CRITICAL"
    if event in _WARNING_EVENTS:
        return "⚠️", "WARNING"
    return "ℹ️", "INFO"

def _priority(event: NotificationEvent) -> int:
    if event in _CRITICAL_EVENTS:
        return 2
    if event in _WARNING_EVENTS:
        return 1
    return 0


def _display(service: ServiceName) -> str:
    mapping = {
        ServiceName.GATEWAY: "IB Gateway",
        ServiceName.BACKEND: "Trading Backend",
        ServiceName.WEBHOOK: "Webhook Ingest",
        ServiceName.DEMO: "Demo Streaming",
        ServiceName.POSTGRES: "PostgreSQL",
        ServiceName.REDIS: "Redis",
    }
    return mapping.get(service, service.value)


def _recovery_owner(service: ServiceName) -> str:
    if service in (ServiceName.GATEWAY, ServiceName.BACKEND, ServiceName.WEBHOOK):
        return "process_manager is responsible for restarting this service."
    if service == ServiceName.DEMO:
        return "systemd will restart demo-streaming.service."
    if service in (ServiceName.POSTGRES, ServiceName.REDIS):
        return "No automatic recovery configured."
    return "systemd is responsible for restarting this supervisor."


def _impact_for(service: ServiceName, event: NotificationEvent, hr: HealthResult | None) -> tuple[str, str | None]:
    if hr and hr.impact:
        return hr.impact, hr.trading_impact
    defaults = {
        ServiceName.GATEWAY: ("Trading Backend cannot communicate with IBKR.", "Order execution is BLOCKED."),
        ServiceName.BACKEND: ("Trading execution is unavailable.", "Trading execution is BLOCKED."),
        ServiceName.WEBHOOK: ("TradingView webhooks cannot be accepted.", "Previously persisted signal_jobs remain in PostgreSQL."),
        ServiceName.DEMO: ("Dashboard streaming may be unavailable.", "None — execution independent."),
        ServiceName.POSTGRES: ("Database access affected.", "Trading execution may be BLOCKED."),
        ServiceName.REDIS: ("Demo Streaming degraded.", "None."),
    }
    imp, trad = defaults.get(service, ("Service degraded.", None))
    return imp, trad


def _event_header(event: NotificationEvent) -> tuple[str, str]:
    """Return (emoji, header_text) for visual hierarchy."""
    mapping: dict[NotificationEvent, tuple[str, str]] = {
        NotificationEvent.START: ("🟢", "WATCHDOG — START"),
        NotificationEvent.STOP: ("⚪", "WATCHDOG — STOPPED"),
        NotificationEvent.FAILURE: ("🔴", "WATCHDOG — SERVICE FAILED"),
        NotificationEvent.UNHEALTHY: ("🟡", "WATCHDOG — DEGRADED"),
        NotificationEvent.RECOVERY_STARTED: ("🟡", "WATCHDOG — RECOVERING"),
        NotificationEvent.RECOVERED: ("🟢", "WATCHDOG — RECOVERED"),
        NotificationEvent.RECOVERY_FAILED: ("🔴", "WATCHDOG — RECOVERY FAILED"),
        NotificationEvent.MANUAL_INTERVENTION_REQUIRED: ("🟠", "WATCHDOG — MANUAL INTERVENTION REQUIRED"),
        NotificationEvent.TRADING_BLOCKED: ("🚨", "WATCHDOG — TRADING BLOCKED"),
    }
    return mapping.get(event, ("🛡️", f"WATCHDOG — {event.value}"))


def format_telegram_message(
    service: ServiceName,
    event: NotificationEvent,
    snapshot: ServiceSnapshot,
    host: str,
    port: int | None = None,
    attempt: str | None = None,
    reason: str | None = None,
    health: HealthResult | None = None,
    recovery_duration: float | None = None,
) -> str:
    health = health or snapshot.last_health
    now = datetime.now(UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    # Use ET for operator display if possible (America/New_York)
    try:
        from zoneinfo import ZoneInfo

        now_et = datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M:%S ET")
    except Exception:
        now_et = now
    what = None
    impact = None
    trading_impact = None
    operator_action = None
    endpoint_url = None
    underlying = None
    pid = None
    log_excerpt = None
    log_marker = None
    if health:
        what = health.what_happened
        impact = health.impact
        trading_impact = health.trading_impact
        operator_action = health.operator_action
        endpoint_url = health.endpoint_url or health.endpoint
        underlying = health.underlying_error or health.detail
        pid = health.pid
        log_excerpt = health.log_excerpt
        log_marker = health.log_marker
        if health.host:
            host = health.host
        if health.port:
            port = health.port
    if not impact:
        impact, trading_impact_fallback = _impact_for(service, event, health)
        if trading_impact is None:
            trading_impact = trading_impact_fallback
    if not what:
        if event == NotificationEvent.FAILURE:
            what = f"{_display(service)} is no longer responding." if service in (ServiceName.BACKEND, ServiceName.WEBHOOK, ServiceName.DEMO) else f"{_display(service)} health check failed."
        elif event == NotificationEvent.UNHEALTHY:
            what = f"{_display(service)} process is alive but is not ready."
        elif event == NotificationEvent.RECOVERY_STARTED:
            what = f"{_display(service)} failed its health check — recovery initiated."
        elif event == NotificationEvent.RECOVERED:
            what = f"{_display(service)} successfully recovered."
        elif event == NotificationEvent.MANUAL_INTERVENTION_REQUIRED:
            what = f"{_display(service)} could not be recovered automatically."
        elif event == NotificationEvent.TRADING_BLOCKED:
            what = "Trading safety gate failed."
        elif event == NotificationEvent.START:
            what = f"{_display(service)} process started."
        elif event == NotificationEvent.STOP:
            what = f"{_display(service)} was intentionally stopped."
        else:
            what = snapshot.failure_reason or (health.detail if health else "")
    if event == NotificationEvent.RECOVERY_STARTED:
        recovery = _recovery_owner(service) + " Recovery attempt initiated."
    elif event == NotificationEvent.RECOVERED:
        recovery = "Recovery verified — health checks now passing."
    elif event == NotificationEvent.MANUAL_INTERVENTION_REQUIRED:
        recovery = f"Recovery attempts exhausted ({attempt or 'MAX'}). Automatic recovery stopped."
    elif event == NotificationEvent.TRADING_BLOCKED:
        recovery = "Trading remains BLOCKED until safety gates pass and operator verifies."
    elif event == NotificationEvent.FAILURE:
        recovery = _recovery_owner(service)
        if attempt:
            recovery += f" Recovery attempt: {attempt}"
    else:
        recovery = _recovery_owner(service) if event in (NotificationEvent.FAILURE, NotificationEvent.UNHEALTHY) else "None — automatic recovery in progress." if attempt else "None."
    if event == NotificationEvent.STOP:
        recovery = "Intentional stop — no automatic recovery."
    emoji, header = _event_header(event)
    # HTML formatting — bold header, <code> for values, separators
    lines: list[str] = []
    lines.append(f"<b>{emoji} {header}</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━")
    lines.append("<b>SERVICE</b>")
    lines.append(f"<code>{_sanitize_for_telegram(_display(service))}</code>")
    lines.append("")
    lines.append("<b>STATUS</b>")
    lines.append(f"<code>{_sanitize_for_telegram(snapshot.state.value)}</code>")
    lines.append("")
    lines.append("<b>EVENT</b>")
    lines.append(f"<code>{_sanitize_for_telegram(event.value)}</code>")
    lines.append("")
    lines.append("<b>DETAILS</b>")
    lines.append(_sanitize_for_telegram(what or "Unknown"))
    lines.append("")
    lines.append("<b>ERROR</b>")
    err_detail = underlying or reason or snapshot.failure_reason or (health.detail if health else "") or "No additional diagnostic information was available."
    if endpoint_url and endpoint_url not in err_detail:
        err_detail = f"{err_detail}\nEndpoint: {endpoint_url}"
    lines.append(f"<code>{_sanitize_for_telegram(err_detail[:500])}</code>")
    if log_marker:
        lines.append(f"<b>EXPECTED</b> <code>{_sanitize_for_telegram(log_marker)}</code>")
    if log_excerpt:
        lines.append("<b>LOG</b>")
        lines.append(f"<code>{_sanitize_for_telegram(log_excerpt[:400])}</code>")
    lines.append("")
    lines.append("<b>WHERE</b>")
    lines.append(f"<code>{_sanitize_for_telegram(host)}</code>" + (f":<code>{port}</code>" if port is not None else ""))
    if pid is not None:
        lines[-1] += f"  PID <code>{pid}</code>"
    if health and health.exit_code is not None:
        lines.append(f"Exit <code>{health.exit_code}</code>")
    if health and health.signal:
        lines.append(f"Signal <code>{health.signal}</code>")
    lines.append("")
    lines.append("<b>IMPACT</b>")
    lines.append(_sanitize_for_telegram(impact or "Unknown"))
    if trading_impact:
        lines.append("")
        lines.append("<b>TRADING</b>")
        lines.append(_sanitize_for_telegram(trading_impact))
    lines.append("")
    lines.append("<b>RECOVERY</b>")
    lines.append(_sanitize_for_telegram(recovery))
    if attempt:
        lines.append("")
        lines.append("<b>ATTEMPT</b>")
        lines.append(f"<code>{_sanitize_for_telegram(attempt)}</code>")
    if recovery_duration is not None:
        lines.append("")
        lines.append("<b>DURATION</b>")
        lines.append(f"<code>{recovery_duration:.1f}s</code>")
    if event == NotificationEvent.RECOVERY_STARTED:
        lines.append("")
        lines.append("<b>VERIFY</b>")
        lines.append("• <code>/health/ready</code> healthy\n• Dependencies reachable")
    lines.append("")
    lines.append("<b>ACTION</b>")
    act = operator_action or _default_action(event, service)
    lines.append(_sanitize_for_telegram(act))
    lines.append("")
    lines.append("<b>TIME</b>")
    lines.append(f"<code>{_sanitize_for_telegram(now_et)}</code>")
    lines.append("━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def _sanitize_for_telegram(text: str) -> str:
    if not text:
        return "Not available"
    t = text[:1000]
    if "TELEGRAM_BOT_TOKEN" in t or "DATABASE_URL" in t or "password" in t.lower():
        return "[REDACTED]"
    return t.replace("<", "&lt;").replace(">", "&gt;") if "<code>" not in t else t


def _default_action(event: NotificationEvent, service: ServiceName) -> str:
    if event == NotificationEvent.MANUAL_INTERVENTION_REQUIRED:
        return "Inspect service logs and resolve authentication/connectivity before clearing blocked state."
    if event == NotificationEvent.TRADING_BLOCKED:
        return "Verify kill switch / safety state and Gateway login before re-enabling trading."
    if event == NotificationEvent.FAILURE:
        return "None — automatic recovery in progress."
    if event == NotificationEvent.STOP:
        return "None — intentional stop."
    return "None."


class NotificationDeduplicator:
    def __init__(self, cooldown_seconds: float = 300.0):
        self.cooldown = cooldown_seconds
        self._last_sent: dict[tuple[str, str], float] = {}

    def should_send(self, service: ServiceName, event: NotificationEvent) -> bool:
        key = (service.value, event.value)
        now = time.monotonic()
        last = self._last_sent.get(key)
        if last is None:
            return True
        if now - last >= self.cooldown:
            return True
        return False

    def mark_sent(self, service: ServiceName, event: NotificationEvent) -> None:
        self._last_sent[(service.value, event.value)] = time.monotonic()


class NotificationQueue:
    """Bounded priority-aware queue: critical reserved, never starved by info."""

    BOUNDED_MAXLEN = 100
    CRITICAL_RESERVED = 20

    def __init__(self, telegram: TelegramClient, settings: WatchdogSettings):
        self.telegram = telegram
        self.settings = settings
        # three priority buckets
        self._critical: deque[tuple[ServiceName, NotificationEvent, str]] = deque()
        self._warning: deque[tuple[ServiceName, NotificationEvent, str]] = deque()
        self._info: deque[tuple[ServiceName, NotificationEvent, str]] = deque()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.dedup = NotificationDeduplicator(cooldown_seconds=settings.notification_cooldown_seconds)
        self.dropped_count: int = 0
        # compat: single queue view for tests that inspect .queue
        self.queue: deque[tuple[ServiceName, NotificationEvent, str]] = deque(maxlen=self.BOUNDED_MAXLEN)

    def _total(self) -> int:
        return len(self._critical) + len(self._warning) + len(self._info)

    def _bucket(self, event: NotificationEvent) -> deque:
        if event in _CRITICAL_EVENTS:
            return self._critical
        if event in _WARNING_EVENTS:
            return self._warning
        return self._info

    @property
    def critical_reserved(self) -> int:
        return self.CRITICAL_RESERVED

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError:
                self._task.cancel()

    def _sync_compat_queue(self) -> None:
        # maintain deque(maxlen=100) view for backward compat tests
        combined = list(self._critical) + list(self._warning) + list(self._info)
        self.queue = deque(combined[-self.BOUNDED_MAXLEN :], maxlen=self.BOUNDED_MAXLEN)

    def enqueue(
        self,
        service: ServiceName,
        event: NotificationEvent,
        text: str,
        force: bool = False,
    ) -> bool:
        if not force and not self.dedup.should_send(service, event):
            logger.debug("Dedup suppressed %s %s", service.value, event.value)
            return False
        prio = _priority(event)
        total = self._total()
        bucket = self._bucket(event)

        if total < self.BOUNDED_MAXLEN:
            bucket.append((service, event, text))
            self.dedup.mark_sent(service, event)
            self._sync_compat_queue()
            return True

        # total full — priority-aware eviction
        if prio == 2:  # critical can evict info then warning
            if len(self._info) > 0:
                self._info.popleft()
                self.dropped_count += 1
                logger.warning("Queue full: evicted INFO for CRITICAL %s %s", service.value, event.value)
            elif len(self._warning) > 0:
                self._warning.popleft()
                self.dropped_count += 1
                logger.warning("Queue full: evicted WARNING for CRITICAL %s %s", service.value, event.value)
            else:
                # only critical left — drop oldest critical (FIFO within priority)
                self._critical.popleft()
                self.dropped_count += 1
                logger.warning("Queue full of CRITICAL: dropped oldest CRITICAL for %s %s", service.value, event.value)
            bucket.append((service, event, text))
            self.dedup.mark_sent(service, event)
            self._sync_compat_queue()
            return True
        elif prio == 1:  # warning can evict info only (never critical)
            if len(self._info) > 0:
                self._info.popleft()
                self.dropped_count += 1
                logger.warning("Queue full: evicted INFO for WARNING %s %s", service.value, event.value)
                bucket.append((service, event, text))
                self.dedup.mark_sent(service, event)
                self._sync_compat_queue()
                return True
            else:
                # cannot evict critical, drop new warning
                self.dropped_count += 1
                logger.warning("Queue full: dropped WARNING %s %s (critical reserved)", service.value, event.value)
                return False
        else:  # info — only evict info, never warning/critical
            if len(self._info) > 0:
                self._info.popleft()
                self.dropped_count += 1
                bucket.append((service, event, text))
                self.dedup.mark_sent(service, event)
                self._sync_compat_queue()
                return True
            else:
                self.dropped_count += 1
                logger.warning("Queue full: dropped INFO %s %s (no info to evict)", service.value, event.value)
                return False

    async def _worker(self) -> None:
        while not self._stop.is_set():
            # priority drain
            item = None
            if self._critical:
                item = self._critical.popleft()
            elif self._warning:
                item = self._warning.popleft()
            elif self._info:
                item = self._info.popleft()
            if item is None:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=1.0)
                except TimeoutError:
                    continue
                continue
            service, event, text = item
            self._sync_compat_queue()
            try:
                ok = await self.telegram.send_message(text)
                if not ok:
                    logger.warning("Telegram send failed for %s %s", service.value, event.value)
            except Exception:
                logger.exception("Telegram worker exception for %s %s", service.value, event.value)
