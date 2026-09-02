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
    ServiceState,
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
    NotificationEvent.MARKET_CLOSED,
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
    if service == ServiceName.GATEWAY:
        return "systemd will restart ibgateway.service automatically (Restart=always, trading-hours controlled)."
    if service == ServiceName.BACKEND:
        return "systemd will restart trading-backend.service automatically (Restart=always, 24/7)."
    if service == ServiceName.WEBHOOK:
        return "systemd will restart webhook-ingest.service automatically when trading session is active."
    if service == ServiceName.DEMO:
        return "systemd will restart demo-streaming.service."
    if service in (ServiceName.POSTGRES, ServiceName.REDIS):
        return "No automatic recovery configured for database service."
    return "systemd is responsible for restarting this supervisor."


def _impact_for(service: ServiceName, event: NotificationEvent, hr: HealthResult | None) -> tuple[str, str | None]:
    if event == NotificationEvent.MARKET_CLOSED:
        defaults = {
            ServiceName.GATEWAY: ("IBKR connectivity is unavailable outside the trading session.", "Trading is unavailable because the market is closed."),
            ServiceName.BACKEND: ("Trading execution is paused outside the trading window.", "Trading is unavailable because the market is closed."),
            ServiceName.WEBHOOK: ("Webhook ingestion is paused outside the trading window; TradingView alerts received outside session will be queued or ignored.", "No trading execution outside session."),
            ServiceName.DEMO: ("Demo dashboard is independent of trading session.", "None."),
            ServiceName.POSTGRES: ("Database remains available.", "None."),
            ServiceName.REDIS: ("Redis remains available.", "None."),
        }
        imp, trad = defaults.get(service, ("Service paused outside trading session.", "Trading unavailable."))
        return imp, trad
    # For TRADING_BLOCKED, always use blocked impact, never health's READY impact
    if event == NotificationEvent.TRADING_BLOCKED:
        defaults = {
            ServiceName.GATEWAY: ("Trading Backend cannot communicate with IBKR paper socket.", "Order execution is BLOCKED."),
            ServiceName.BACKEND: ("Execution API and worker pool are unavailable or safety gate blocked.", "Trading is BLOCKED."),
            ServiceName.WEBHOOK: ("TradingView webhooks cannot be accepted on port 8000.", "No direct impact on active executions; new signals cannot be ingested. Previously persisted signal_jobs remain safe in PostgreSQL."),
            ServiceName.DEMO: ("Dashboard streaming UI unavailable on port 8010.", "None — execution pipeline is independent of Demo Streaming."),
            ServiceName.POSTGRES: ("Database session queries failed.", "Trading execution is BLOCKED."),
            ServiceName.REDIS: ("Demo Streaming SSE pub/sub unavailable.", "None — execution pipeline is independent of Redis."),
        }
        imp, trad = defaults.get(service, ("Service degraded.", "Trading is BLOCKED."))
        return imp, trad
    if hr and hr.impact and event not in (NotificationEvent.START, NotificationEvent.RECOVERED):
        # Do not propagate a healthy READY trading_impact into a blocked/failed notification
        trading = hr.trading_impact
        if trading and "READY" in trading and event in (NotificationEvent.FAILURE, NotificationEvent.UNHEALTHY, NotificationEvent.TRADING_BLOCKED, NotificationEvent.RECOVERY_FAILED, NotificationEvent.MANUAL_INTERVENTION_REQUIRED):
            # override with blocked-specific impact instead of misleading READY
            return hr.impact, None  # will fallback to defaults below
        return hr.impact, hr.trading_impact
    if event in (NotificationEvent.START, NotificationEvent.RECOVERED):
        if service == ServiceName.GATEWAY:
            return "Gateway health confirmed.", "Gateway health confirmed; overall trading readiness is determined by safety gates."
        if service == ServiceName.BACKEND:
            return "Backend health confirmed.", "Backend health confirmed; overall trading readiness is determined by safety gates."
        if service == ServiceName.POSTGRES:
            return "Database session connected.", "No direct trading conclusion from this health check."
        if service == ServiceName.REDIS:
            return "Redis ping OK.", "No direct trading impact."
        if service in (ServiceName.WEBHOOK, ServiceName.DEMO):
            return "Service is operational.", "Execution independent."
        return "Service health confirmed.", "Trading readiness not evaluated by this notification."
    defaults = {
        ServiceName.GATEWAY: ("Trading Backend cannot communicate with IBKR paper socket.", "Order execution may be affected."),
        ServiceName.BACKEND: ("Execution API and worker pool are unavailable.", "Order execution may be affected."),
        ServiceName.WEBHOOK: ("TradingView webhooks cannot be accepted on port 8000.", "No direct impact on active executions; new signals cannot be ingested. Previously persisted signal_jobs remain safe in PostgreSQL."),
        ServiceName.DEMO: ("Dashboard streaming UI unavailable on port 8010.", "None — execution pipeline is independent of Demo Streaming."),
        ServiceName.POSTGRES: ("Database session queries failed.", "Order execution and webhook ingestion may be affected."),
        ServiceName.REDIS: ("Demo Streaming SSE pub/sub unavailable.", "None — execution pipeline is independent of Redis."),
    }
    imp, trad = defaults.get(service, ("Service degraded.", None))
    return imp, trad


def _is_success_detail(text: str) -> bool:
    """Return True if text represents a successful health check, never an error."""
    t = text.strip().lower()
    if t in ("healthy", "ok", "select 1 ok", "ping ok", "http 200", "http 200 → ok"):
        return True
    # Pure TCP open without failure qualifier
    if t in ("tcp 127.0.0.1:4002 open", "tcp 127.0.0.1:4002 open → ok", "tcp 127.0.0.1:8001 open", "tcp 127.0.0.1:8000 open", "tcp 127.0.0.1:5432 open", "tcp 127.0.0.1:6379 open", "tcp 127.0.0.1:8010 open"):
        return True
    if t.startswith("tcp ") and "open" in t and "refused" not in t and "failed" not in t:
        # If contains failure qualifier, not pure success
        if "(" in t or "login" in t or "missing" in t or "not seen" in t or "degraded" in t:
            return False
        # e.g. "tcp 127.0.0.1:4002 open" -> success, but "tcp 127.0.0.1:4002 open (login marker not seen)" -> not success
        return bool(t.count("open") == 1 and len(t) < 40)


def _check_summary(service: ServiceName, event: NotificationEvent, health: HealthResult | None, port: int | None) -> str:
    if event == NotificationEvent.MARKET_CLOSED:
        return "Trading window → CLOSED (outside 09:30–16:00 ET)"
    # Structured: base on health.status, not string matching
    if health:
        from app.services.watchdog.models import HealthStatus as _HS
        if health.status == _HS.HEALTHY:
            # success case — never treat success as failure
            if service == ServiceName.POSTGRES:
                return "SELECT 1 → OK"
            if service == ServiceName.REDIS:
                return "PING → OK"
            if service == ServiceName.GATEWAY:
                return f"TCP 127.0.0.1:{port or health.port or 4002} → OPEN"
            # backend/webhook/demo: HTTP 200
            if health.detail and "HTTP" in health.detail:
                return f"{health.detail} → OK" if "→" not in health.detail else health.detail
            return "HTTP 200 → OK"
        # degraded/failed — show actual detail
        if health.detail:
            if health.status == _HS.DEGRADED:
                return f"Health check → {health.detail}"
            return f"Health check → {health.detail}"
    if event in (NotificationEvent.START, NotificationEvent.RECOVERED):
        if service == ServiceName.POSTGRES:
            return "SELECT 1 → OK"
        if service == ServiceName.REDIS:
            return "PING → OK"
        if service == ServiceName.GATEWAY:
            return f"TCP 127.0.0.1:{port or 4002} → OPEN"
        return "HTTP 200 → OK"
    else:
        if health and health.detail:
            return f"Health check → {health.detail}"
        return f"Health check on port {port} → Failed" if port else "Health check → Failed"


# Authoritative event semantics (observed facts, not assumptions):
# START: watchdog observed service healthy on initial check (did NOT start it)
# STOP: watchdog itself stopped gracefully (SIGTERM), not service crash
# FAILURE: health endpoint unreachable / TCP refused — watchdog could not reach service (did NOT claim crash)
# UNHEALTHY: process alive (TCP open) but readiness failed — degraded
# RECOVERY_STARTED: watchdog detected unhealthy and notes process_manager will attempt recovery (watchdog did NOT restart)
# RECOVERED: watchdog verified service healthy again after previous unhealthy/manual state (did NOT claim restart)
# RECOVERY_FAILED: verification still failed
# MANUAL_INTERVENTION_REQUIRED: budget exhausted — automatic recovery stopped, operator required (does NOT mean currently unhealthy forever)
# TRADING_BLOCKED: safety gate failed — trading must remain blocked
# MARKET_CLOSED: service intentionally stopped outside trading window (weekdays 09:30-16:00 ET) — not a failure
def _event_header(event: NotificationEvent) -> tuple[str, str]:
    """Return (emoji, header_text) for visual hierarchy — user-facing, precise."""
    mapping: dict[NotificationEvent, tuple[str, str]] = {
        NotificationEvent.START: ("🟢", "WATCHDOG — HEALTHY"),
        NotificationEvent.STOP: ("⚪", "WATCHDOG — STOPPED"),
        NotificationEvent.FAILURE: ("🔴", "WATCHDOG — HEALTH CHECK FAILED"),
        NotificationEvent.UNHEALTHY: ("🟡", "WATCHDOG — DEGRADED"),
        NotificationEvent.RECOVERY_STARTED: ("🟡", "WATCHDOG — RECOVERING"),
        NotificationEvent.RECOVERED: ("🟢", "WATCHDOG — RECOVERED"),
        NotificationEvent.RECOVERY_FAILED: ("🔴", "WATCHDOG — RECOVERY FAILED"),
        NotificationEvent.MANUAL_INTERVENTION_REQUIRED: ("🟠", "WATCHDOG — MANUAL ACTION REQUIRED"),
        NotificationEvent.TRADING_BLOCKED: ("🚨", "WATCHDOG — TRADING BLOCKED"),
        NotificationEvent.MARKET_CLOSED: ("🟡", "WATCHDOG — MARKET CLOSED"),
    }
    return mapping.get(event, ("🛡️", f"WATCHDOG — {event.value}"))


def _trading_status_for(service: ServiceName, event: NotificationEvent, health: HealthResult | None, snapshot: ServiceSnapshot) -> str:
    """Derive trading readiness: READY / BLOCKED / NOT AFFECTED / UNKNOWN / MARKET_CLOSED."""
    if event == NotificationEvent.MARKET_CLOSED:
        return "MARKET CLOSED"
    # Non-trading services never affect execution
    if service in (ServiceName.DEMO, ServiceName.REDIS):
        return "NOT AFFECTED"
    if service == ServiceName.WEBHOOK:
        # webhook ingest failure does not block active execution, but blocks new signals
        if event in (NotificationEvent.FAILURE, NotificationEvent.UNHEALTHY, NotificationEvent.TRADING_BLOCKED):
            return "NEW SIGNALS BLOCKED (execution independent)"
        if event == NotificationEvent.MARKET_CLOSED:
            return "MARKET CLOSED"
        return "NOT AFFECTED"
    # Trading-critical: gateway, backend, postgres
    if event == NotificationEvent.TRADING_BLOCKED:
        return "BLOCKED"
    if health:
        from app.services.watchdog.models import HealthStatus as _HS
        if health.reason == "readiness_unconfirmed":
            return "MONITORING UNCONFIRMED (execution active)"
        if health.status != _HS.HEALTHY:
            return "BLOCKED"
    if snapshot.state.value in ("TRADING_BLOCKED", "MARKET_CLOSED"):
        return "BLOCKED" if snapshot.state.value == "TRADING_BLOCKED" else "MARKET CLOSED"
    if event in (NotificationEvent.FAILURE, NotificationEvent.UNHEALTHY, NotificationEvent.RECOVERY_FAILED, NotificationEvent.MANUAL_INTERVENTION_REQUIRED):
        return "BLOCKED"
    if event in (NotificationEvent.START, NotificationEvent.RECOVERED):
        return "READY (subject to safety gates)"
    return "UNKNOWN"


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
    event_timestamp: datetime | None = None,
) -> str:
    health = health or snapshot.last_health
    # Use provided event_timestamp for ordering, else now
    ts = event_timestamp or datetime.now(UTC)
    now = ts.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    # Use ET for operator display if possible (America/New_York)
    try:
        from zoneinfo import ZoneInfo

        now_et = ts.astimezone(ZoneInfo("America/New_York")).strftime("%H:%M:%S ET")
    except Exception:
        now_et = now
    what = None
    impact = None
    trading_impact = None
    operator_action = None
    endpoint_url = None
    underlying_error_only = None
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
        underlying_error_only = health.underlying_error
        # Do NOT use health.detail as error when status is HEALTHY — structured success
        from app.services.watchdog.models import HealthStatus as _HS
        if health.status != _HS.HEALTHY:
            underlying = health.underlying_error or health.detail
        else:
            underlying = health.underlying_error  # None for healthy => no error
        pid = health.pid
        log_excerpt = health.log_excerpt
        log_marker = health.log_marker
        if health.host:
            host = health.host
        if health.port:
            port = health.port
    # Service status vs trading status distinction
    from app.services.watchdog.models import HealthStatus as _HS
    if event == NotificationEvent.MARKET_CLOSED:
        service_status = ServiceState.MARKET_CLOSED.value
    elif health:
        service_status = health.status.value
    else:
        service_status = snapshot.state.value
    trading_status = _trading_status_for(service, event, health, snapshot)
    # Override impact/trading_impact for MARKET_CLOSED as well
    if event == NotificationEvent.MARKET_CLOSED:
        impact, trading_impact = _impact_for(service, event, None)
    # Override impact/trading_impact for contradictory cases
    # For TRADING_BLOCKED with healthy service, health's READY impact is misleading — use blocked impact
    elif event == NotificationEvent.TRADING_BLOCKED:
        # force blocked impact regardless of health.trading_impact
        impact, trading_impact = _impact_for(service, event, None)
        # preserve health impact detail for DETAILS explanation but not for IMPACT
    elif not impact:
        impact, trading_impact_fallback = _impact_for(service, event, health)
        if trading_impact is None:
            trading_impact = trading_impact_fallback
    # For TRADING_BLOCKED with healthy health but safety gate reason, clarify trading_impact
    if event == NotificationEvent.TRADING_BLOCKED:
        if not trading_impact or "READY" in (trading_impact or ""):
            trading_impact = trading_status
    # DETAILS: separate service health from trading readiness
    # MARKET_CLOSED honest messaging
    if event == NotificationEvent.MARKET_CLOSED:
        what = f"{_display(service)} is intentionally stopped because the market is closed (weekdays 09:30–16:00 ET). No action required."
    # If not already set, derive accurate what_happened
    elif event == NotificationEvent.TRADING_BLOCKED:
        if health and health.status == _HS.HEALTHY:
            reason_text = snapshot.failure_reason or reason or "safety gate not cleared"
            what = f"{_display(service)} health check is passing, but trading remains blocked because safety gate has not been cleared: {reason_text}."
        else:
            if not what or what == "Trading safety gate failed.":
                reason_text = snapshot.failure_reason or reason or health.underlying_error if health else "unknown"
                what = f"Trading safety gate failed: {reason_text}"
    if not what:
        if event == NotificationEvent.FAILURE:
            what = f"Watchdog could not reach {_display(service)} on {host}:{port}." if port else f"Watchdog could not reach {_display(service)}."
        elif event == NotificationEvent.UNHEALTHY:
            what = f"{_display(service)} process is alive but readiness check failed."
        elif event == NotificationEvent.RECOVERY_STARTED:
            what = f"{_display(service)} failed health check — recovery will be attempted."
        elif event == NotificationEvent.RECOVERED:
            if snapshot.failure_reason and "manual" in snapshot.failure_reason.lower():
                what = f"Watchdog confirmed {_display(service)} is healthy again after manual intervention was previously required."
            else:
                what = f"Watchdog verified {_display(service)} is healthy again after a previous unhealthy state."
        elif event == NotificationEvent.MANUAL_INTERVENTION_REQUIRED:
            what = f"Automatic recovery attempts for {_display(service)} are exhausted."
        elif event == NotificationEvent.TRADING_BLOCKED:
            what = "Trading safety gate failed."
        elif event == NotificationEvent.START:
            what = f"Watchdog confirmed {_display(service)} is healthy."
        elif event == NotificationEvent.STOP:
            what = f"Watchdog monitor for {_display(service)} was intentionally stopped."
        else:
            what = snapshot.failure_reason or (health.detail if health else "")
    if event == NotificationEvent.MARKET_CLOSED:
        recovery = "No recovery required — service will start automatically at next trading session (weekdays 09:30 ET / 19:00 IST)."
    elif event == NotificationEvent.RECOVERY_STARTED:
        recovery = f"Recovery workflow started. {_recovery_owner(service)}"
    elif event == NotificationEvent.RECOVERED:
        recovery = "Health recovery verified by watchdog."
    elif event == NotificationEvent.MANUAL_INTERVENTION_REQUIRED:
        recovery = f"Automatic recovery attempts exhausted ({attempt or '5/5'}). Manual intervention is required."
    elif event == NotificationEvent.TRADING_BLOCKED:
        recovery = "Trading remains BLOCKED until safety gates pass and operator clears."
    elif event == NotificationEvent.FAILURE:
        recovery = _recovery_owner(service)
        if attempt:
            recovery += f" Recovery attempt: {attempt}"
    else:
        recovery = _recovery_owner(service) if event in (NotificationEvent.FAILURE, NotificationEvent.UNHEALTHY) else None
    if event == NotificationEvent.STOP:
        recovery = "Watchdog monitor stopped — no automatic recovery."
    emoji, header = _event_header(event)
    # User-facing event value: NotificationEvent.START displays as HEALTH CONFIRMED
    event_disp = "HEALTH CONFIRMED" if event == NotificationEvent.START else event.value
    check_summary = _check_summary(service, event, health, port)

    # Determine whether an ERROR field is appropriate (only for non-healthy events with actual error)
    # Structured: never put successful check into ERROR
    err_detail = None
    if event in (NotificationEvent.FAILURE, NotificationEvent.UNHEALTHY, NotificationEvent.RECOVERY_STARTED, NotificationEvent.RECOVERY_FAILED, NotificationEvent.MANUAL_INTERVENTION_REQUIRED, NotificationEvent.TRADING_BLOCKED):
        # For healthy TRADING_BLOCKED, error is safety gate reason, not health detail
        from app.services.watchdog.models import HealthStatus as _HS2
        is_healthy = health is not None and health.status == _HS2.HEALTHY
        if is_healthy and event == NotificationEvent.TRADING_BLOCKED:
            raw_err = snapshot.failure_reason or reason
            # need to ensure raw_err is not a success string
            if raw_err and _is_success_detail(raw_err):
                raw_err = None
        elif is_healthy:
            # healthy should not have error for FAILURE/UNHEALTHY — but those events shouldn't happen with healthy
            raw_err = None
        else:
            # unhealthy/failed: prefer underlying error, then failure_reason
            raw_err = underlying_error_only or snapshot.failure_reason or reason
            if not raw_err and health and health.detail and not _is_success_detail(health.detail):
                raw_err = health.detail
        if raw_err and not _is_success_detail(raw_err):
            err_detail = raw_err
            # only attach endpoint for actual failures, not for healthy blocked
            if endpoint_url and endpoint_url not in err_detail and not is_healthy:
                err_detail = f"{err_detail}\nEndpoint: {endpoint_url}"

    # HTML formatting — bold header, <code> for values, separators
    lines: list[str] = []
    lines.append(f"<b>{emoji} {header}</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━")
    lines.append("<b>SERVICE</b>")
    lines.append(f"<code>{_sanitize_for_telegram(_display(service))}</code>")
    lines.append("")
    # Separate SERVICE health from TRADING readiness (prompt §3)
    lines.append("<b>SERVICE STATUS</b>")
    lines.append(f"<code>{_sanitize_for_telegram(service_status)}</code>")
    lines.append("")
    lines.append("<b>TRADING STATUS</b>")
    lines.append(f"<code>{_sanitize_for_telegram(trading_status)}</code>")
    lines.append("")
    # Keep legacy STATUS for backward compat tests that search for "STATUS"
    lines.append("<b>STATUS</b>")
    lines.append(f"<code>{_sanitize_for_telegram(snapshot.state.value)}</code>")
    lines.append("")
    lines.append("<b>EVENT</b>")
    lines.append(f"<code>{_sanitize_for_telegram(event_disp)}</code>")
    lines.append("")
    lines.append("<b>DETAILS</b>")
    lines.append(_sanitize_for_telegram(what or "Unknown"))
    lines.append("")
    lines.append("<b>CHECK</b>")
    lines.append(f"<code>{_sanitize_for_telegram(check_summary)}</code>")
    if err_detail:
        lines.append("")
        lines.append("<b>ERROR</b>")
        lines.append(f"<code>{_sanitize_for_telegram(err_detail[:500])}</code>")
    if log_marker:
        lines.append("")
        lines.append(f"<b>EXPECTED</b> <code>{_sanitize_for_telegram(log_marker)}</code>")
    if log_excerpt:
        lines.append("")
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
    if recovery:
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
        # Service-specific verification — avoid implying unrelated endpoint is evidence for failed service
        if service == ServiceName.GATEWAY:
            lines.append("• <code>TCP 127.0.0.1:4002</code> open\n• <code>Login has completed</code> in ib_gateway.log\n• Xvfb display :99 running")
        elif service == ServiceName.BACKEND:
            lines.append("• <code>Trading Backend /health/ready</code> healthy (DB + TWS)\n• Safety gates <code>SAFE</code>\n• Dependencies reachable")
        elif service == ServiceName.WEBHOOK:
            lines.append("• <code>Webhook /health/ready</code> healthy\n• PostgreSQL <code>SELECT 1</code> ok")
        elif service == ServiceName.POSTGRES:
            lines.append("• PostgreSQL <code>SELECT 1</code> ok")
        elif service == ServiceName.REDIS:
            lines.append("• Redis <code>PING</code> ok")
        elif service == ServiceName.DEMO:
            lines.append("• <code>Demo /health</code> healthy\n• Redis reachable")
        else:
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
    low = t.lower()
    if "telegram_bot_token" in low or "database_url" in low or "password" in low or "api_key" in low or "secret" in low or "bot_token" in low or "chat_id" in low:
        return "[REDACTED]"
    return t.replace("<", "&lt;").replace(">", "&gt;") if "<code>" not in t else t


def _default_action(event: NotificationEvent, service: ServiceName) -> str:
    if event == NotificationEvent.MARKET_CLOSED:
        return "No action required. Service will start automatically when the next trading session begins."
    if event == NotificationEvent.MANUAL_INTERVENTION_REQUIRED:
        return "Operator investigation required. Resolve root cause before clearing blocked state."
    if event == NotificationEvent.TRADING_BLOCKED:
        return "Verify kill switch / safety state and Gateway login before re-enabling trading."
    if event == NotificationEvent.FAILURE:
        if service in (ServiceName.POSTGRES, ServiceName.REDIS):
            return "Check database process/container manually."
        return f"Automatic recovery will be attempted by {_recovery_owner(service).split(' ')[0]}."
    if event == NotificationEvent.STOP:
        return "None — watchdog stopped."
    if event == NotificationEvent.START:
        return "Watchdog confirmed health. No action taken by watchdog."
    if event == NotificationEvent.RECOVERED:
        return "None. Watchdog verified service is healthy."
    return "None."


def format_resource_alert(
    resource_type: str,
    state: str,
    usage_percent: float,
    threshold: float,
    total_bytes: int | None = None,
    used_bytes: int | None = None,
    available_bytes: int | None = None,
    mount: str | None = None,
    extra: dict | None = None,
    is_recovery: bool = False,
) -> str:
    """Format a resource alert (CPU/Memory/Storage/Inodes) for Telegram."""
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    now = datetime.now(UTC)
    try:
        now_et = now.astimezone(ZoneInfo("America/New_York")).strftime("%H:%M:%S ET")
    except Exception:
        now_et = now.strftime("%H:%M:%S UTC")

    if is_recovery or state == "NORMAL":
        emoji, header = "🟢", "WATCHDOG — SYSTEM RESOURCE RECOVERED"
    elif state == "WARNING":
        emoji, header = "🟠", "WATCHDOG — SYSTEM RESOURCE WARNING"
    elif state == "CRITICAL":
        emoji, header = "🔴", "WATCHDOG — SYSTEM RESOURCE CRITICAL"
    else:
        emoji, header = "🟡", f"WATCHDOG — SYSTEM RESOURCE {state}"

    def _fmt_bytes(b: int | None) -> str:
        if b is None:
            return "N/A"
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if abs(b) < 1024:
                return f"{b:.1f} {unit}"
            b /= 1024
        return f"{b:.1f} PB"

    lines: list[str] = []
    lines.append(f"<b>{emoji} {header}</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━")
    lines.append("<b>RESOURCE</b>")
    lines.append(f"<code>{_sanitize_for_telegram(resource_type)}</code>")
    lines.append("")
    lines.append("<b>STATUS</b>")
    lines.append(f"<code>{_sanitize_for_telegram(state)}</code>")
    lines.append("")
    lines.append("<b>USAGE</b>")
    lines.append(f"<code>{usage_percent:.1f}%</code>")
    lines.append("")
    lines.append("<b>THRESHOLD</b>")
    lines.append(f"<code>{threshold:.1f}%</code>")
    if mount:
        lines.append("")
        lines.append("<b>MOUNT</b>")
        lines.append(f"<code>{_sanitize_for_telegram(mount)}</code>")
    if total_bytes is not None:
        lines.append("")
        lines.append("<b>TOTAL</b>")
        lines.append(f"<code>{_fmt_bytes(total_bytes)}</code>")
    if used_bytes is not None:
        lines.append("")
        lines.append("<b>USED</b>")
        lines.append(f"<code>{_fmt_bytes(used_bytes)}</code>")
    if available_bytes is not None:
        lines.append("")
        lines.append("<b>AVAILABLE</b>")
        lines.append(f"<code>{_fmt_bytes(available_bytes)}</code>")
    if extra:
        # Show load avg for CPU, etc.
        if "load_avg" in extra:
            lines.append("")
            lines.append("<b>LOAD</b>")
            lines.append(f"<code>{extra['load_avg']}</code>")
        if "cpu_count" in extra:
            lines.append("")
            lines.append("<b>CPU COUNT</b>")
            lines.append(f"<code>{extra['cpu_count']}</code>")
        if extra.get("top_processes"):
            lines.append("")
            lines.append("<b>TOP CPU PROCESSES</b>")
            lines.append("<code>Top CPU processes at detection</code>")
            for idx, proc in enumerate(extra["top_processes"][:3], 1):
                # Sanitize already, but double-check
                name = _sanitize_for_telegram(str(proc.get("name", "unknown")))
                pid = proc.get("pid", "?")
                cpu = proc.get("cpu_percent", 0)
                lines.append(f"{idx}. <code>{name}</code> — PID <code>{pid}</code> — <code>{cpu:.1f}%</code>")
    lines.append("")
    lines.append("<b>DETAILS</b>")
    if is_recovery:
        lines.append(_sanitize_for_telegram(f"{resource_type} usage has recovered to {usage_percent:.1f}% (below recovery threshold {threshold:.1f}%)."))
    elif state == "CRITICAL":
        lines.append(_sanitize_for_telegram(f"{resource_type} usage has exceeded critical threshold ({usage_percent:.1f}% >= {threshold:.1f}%). Immediate attention recommended."))
    else:
        lines.append(_sanitize_for_telegram(f"{resource_type} usage has exceeded warning threshold ({usage_percent:.1f}% >= {threshold:.1f}%)."))
    lines.append("")
    lines.append("<b>ACTION</b>")
    if is_recovery:
        lines.append(_sanitize_for_telegram("No action required. Resource has recovered."))
    elif state == "CRITICAL":
        lines.append(_sanitize_for_telegram("Investigate resource-consuming processes immediately; consider freeing space/memory or scaling."))
    else:
        lines.append(_sanitize_for_telegram("Investigate resource-consuming processes if usage remains elevated."))
    lines.append("")
    lines.append("<b>TIME</b>")
    lines.append(f"<code>{_sanitize_for_telegram(now_et)}</code>")
    lines.append("━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


class NotificationDeduplicator:
    def __init__(self, cooldown_seconds: float = 300.0):
        self.cooldown = cooldown_seconds
        self._last_sent: dict[tuple[str, str], float] = {}
        # track last event per service to avoid over-dedup of distinct transitions
        self._last_event_per_service: dict[str, NotificationEvent] = {}

    def should_send(self, service: ServiceName, event: NotificationEvent) -> bool:
        key = (service.value, event.value)
        now = time.monotonic()
        last = self._last_sent.get(key)
        if last is None:
            return True
        # If last event for this service was different, allow even within cooldown
        # — prevents collapsing legitimate sequences like FAILURE -> RECOVERING -> RECOVERED
        # into one due to same-event cooldown.
        last_ev = self._last_event_per_service.get(service.value)
        if last_ev is not None and last_ev != event:
            # Different event type — not a duplicate of same unchanged state
            return True
        if now - last >= self.cooldown:
            return True
        return False

    def mark_sent(self, service: ServiceName, event: NotificationEvent) -> None:
        self._last_sent[(service.value, event.value)] = time.monotonic()
        self._last_event_per_service[service.value] = event

    def clear(self, service: ServiceName) -> None:
        # Clear dedup state on recovery — allows new failure cycle to notify promptly
        self._last_event_per_service.pop(service.value, None)


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
