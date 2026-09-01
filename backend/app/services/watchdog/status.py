"""Build /status Telegram response — compact but operationally complete.

Reuses watchdog snapshots, resource monitor, and system info. No side-effects.
Never exposes secrets.
"""

from __future__ import annotations

import platform
import socket
import time
from datetime import UTC, datetime, time as dtime
from zoneinfo import ZoneInfo

import psutil

from app.services.watchdog.config import WatchdogSettings
from app.services.watchdog.models import ServiceName, ServiceSnapshot, ServiceState
from app.services.watchdog.resources import ResourceMonitor, ResourceState, ResourceType


def _sanitize(text: str | None) -> str:
    if not text:
        return ""
    low = text.lower()
    if any(k in low for k in ["telegram_bot_token", "database_url", "password", "api_key", "secret", "bot_token", "chat_id"]):
        return "[REDACTED]"
    # Escape HTML
    return text.replace("<", "&lt;").replace(">", "&gt;")[:200]


def _is_trading_session(now: datetime | None = None) -> bool:
    tz = ZoneInfo("America/New_York")
    now_et = (now or datetime.now(tz)).astimezone(tz)
    if now_et.weekday() >= 5:
        return False
    return dtime(9, 30) <= now_et.time() < dtime(16, 0)


def _next_open(now: datetime | None = None) -> str:
    tz = ZoneInfo("America/New_York")
    now_et = (now or datetime.now(tz)).astimezone(tz)
    # Simple: next weekday 09:30 ET
    candidate = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    if now_et.time() < dtime(9, 30) and now_et.weekday() < 5:
        return candidate.strftime("%H:%M ET (%Y-%m-%d)")
    # Move to next weekday
    days_ahead = 1
    while True:
        nxt = now_et.replace(hour=9, minute=30, second=0, microsecond=0) + __import__("datetime").timedelta(days=days_ahead)
        # Handle DST via astimezone
        nxt = nxt.astimezone(tz)
        if nxt.weekday() < 5:
            return nxt.strftime("%H:%M ET (%Y-%m-%d)")
        days_ahead += 1


def _fmt_bytes(b: int | None) -> str:
    if b is None:
        return "N/A"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(b) < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def _service_emoji(state: ServiceState) -> str:
    mapping = {
        ServiceState.HEALTHY: "🟢",
        ServiceState.MARKET_CLOSED: "🟡",
        ServiceState.DEGRADED: "🟡",
        ServiceState.FAILED: "🔴",
        ServiceState.RECOVERING: "🟡",
        ServiceState.VERIFYING: "🟡",
        ServiceState.RECOVERED: "🟢",
        ServiceState.TRADING_BLOCKED: "🟠",
        ServiceState.MANUAL_INTERVENTION_REQUIRED: "🔴",
        ServiceState.UNKNOWN: "⚪",
        ServiceState.STARTING: "⚪",
    }
    return mapping.get(state, "⚪")


def _resource_emoji(state: ResourceState) -> str:
    if state == ResourceState.CRITICAL:
        return "🔴"
    if state == ResourceState.WARNING:
        return "🟠"
    if state == ResourceState.NORMAL:
        return "🟢"
    return "⚪"


def build_status_message(
    snapshots: dict[ServiceName, ServiceSnapshot],
    resource_monitor: ResourceMonitor | None,
    settings: WatchdogSettings,
    include_process_details: bool = True,
) -> str:
    now_utc = datetime.now(UTC)
    try:
        now_et = now_utc.astimezone(ZoneInfo("America/New_York"))
        now_ist = now_utc.astimezone(ZoneInfo("Asia/Kolkata"))
    except Exception:
        now_et = now_utc
        now_ist = now_utc

    is_open = _is_trading_session(now_utc)
    market_emoji = "🟢" if is_open else "🔴"
    market_text = "OPEN" if is_open else "CLOSED"

    # Overall status
    # If market closed and trading services are MARKET_CLOSED, overall is MARKET_CLOSED, not CRITICAL
    overall = "HEALTHY"
    # Check trading services: gateway/backend/webhook should be MARKET_CLOSED when closed, HEALTHY when open
    trading_services = [ServiceName.GATEWAY, ServiceName.BACKEND, ServiceName.WEBHOOK]
    infra_services = [ServiceName.POSTGRES, ServiceName.REDIS, ServiceName.DEMO]
    # Determine worst state among relevant services
    worst = "HEALTHY"
    for svc in trading_services + infra_services:
        snap = snapshots.get(svc)
        if not snap:
            continue
        st = snap.state
        if st in (ServiceState.FAILED, ServiceState.MANUAL_INTERVENTION_REQUIRED):
            # But if market closed and svc is trading, MARKET_CLOSED is not failure
            if svc in trading_services and st == ServiceState.MARKET_CLOSED:
                continue
            worst = "CRITICAL"
            break
        if st in (ServiceState.DEGRADED, ServiceState.TRADING_BLOCKED, ServiceState.RECOVERING, ServiceState.VERIFYING):
            if worst != "CRITICAL":
                worst = "DEGRADED"
    # Resource worst
    if resource_monitor:
        for rtype in [ResourceType.CPU, ResourceType.MEMORY, ResourceType.STORAGE, ResourceType.INODES]:
            rs = resource_monitor.get_state(rtype)
            if rs == ResourceState.CRITICAL:
                worst = "CRITICAL"
                break
            if rs == ResourceState.WARNING and worst == "HEALTHY":
                worst = "DEGRADED"
    if worst == "HEALTHY" and not is_open:
        # Market closed with all expected stops is not failure, but show market closed
        overall = "MARKET_CLOSED"
    elif worst == "HEALTHY":
        overall = "HEALTHY"
    elif worst == "DEGRADED":
        overall = "DEGRADED"
    elif worst == "CRITICAL":
        overall = "CRITICAL"
    else:
        overall = worst

    overall_emoji = {"HEALTHY": "🟢", "DEGRADED": "🟠", "CRITICAL": "🔴", "MARKET_CLOSED": "🟡"}.get(overall, "🟡")

    lines: list[str] = []
    lines.append(f"<b>{overall_emoji} SYSTEM STATUS — {overall}</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━")
    lines.append("<b>TIME</b>")
    try:
        lines.append(f"<code>{now_et.strftime('%H:%M:%S %Z')}</code> / <code>{now_utc.strftime('%H:%M:%S UTC')}</code> / <code>{now_ist.strftime('%H:%M:%S IST')}</code>")
    except Exception:
        lines.append(f"<code>{now_utc.strftime('%H:%M:%S UTC')}</code>")
    lines.append("")
    lines.append("<b>MARKET</b>")
    lines.append(f"{market_emoji} <code>{market_text}</code>")
    lines.append(f"Session: <code>09:30–16:00 ET</code> (Mon-Fri, America/New_York)")
    if not is_open:
        lines.append(f"Next Open: <code>{_next_open(now_utc)}</code> / <code>19:00 IST</code>" if now_et.tzinfo else "Next Open: <code>09:30 ET</code>")
    lines.append("")

    # Trading services
    lines.append("━━━━━━━━━━━━━━━━━━━")
    lines.append("<b>TRADING SERVICES</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━")
    for svc in trading_services:
        snap = snapshots.get(svc)
        if not snap:
            continue
        disp = {
            ServiceName.GATEWAY: "IB Gateway",
            ServiceName.BACKEND: "Trading Backend",
            ServiceName.WEBHOOK: "Webhook Ingest",
        }.get(svc, svc.value)
        emoji = _service_emoji(snap.state)
        # Port
        port = {"gateway": 4002, "backend": 8001, "webhook": 8000}.get(svc.value, 0)
        hr = snap.last_health
        extra = ""
        if svc == ServiceName.GATEWAY and hr:
            # Xvfb/Gateway detail
            if hr.reason == "xvfb_missing":
                extra = " | Xvfb: STOPPED"
            elif hr.status and hr.status.value == "HEALTHY":
                extra = " | Xvfb: RUNNING"
        lines.append(f"{emoji} <b>{disp}</b> — <code>{snap.state.value}</code> (:{port}){extra}")
        if snap.state == ServiceState.MARKET_CLOSED:
            lines.append("  <code>Expected outside session</code>")
        elif hr and hr.detail:
            detail = _sanitize(hr.detail[:80])
            lines.append(f"  <code>{detail}</code>")
    lines.append("")

    # Application services
    lines.append("━━━━━━━━━━━━━━━━━━━")
    lines.append("<b>APPLICATION SERVICES</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━")
    for svc in [ServiceName.DEMO, ServiceName.POSTGRES, ServiceName.REDIS]:
        snap = snapshots.get(svc)
        if not snap:
            continue
        disp = {ServiceName.DEMO: "Demo Streaming", ServiceName.POSTGRES: "PostgreSQL", ServiceName.REDIS: "Redis"}.get(svc, svc.value)
        emoji = _service_emoji(snap.state)
        hr = snap.last_health
        detail = ""
        if hr and hr.detail:
            detail = _sanitize(hr.detail[:60])
        # PID
        pid_str = f" PID:{hr.pid}" if hr and hr.pid else ""
        lines.append(f"{emoji} <b>{disp}</b> — <code>{snap.state.value}</code>{pid_str}")
        if detail:
            lines.append(f"  <code>{detail}</code>")
    # Watchdog and process-manager via psutil
    if include_process_details:
        try:
            for name, pattern in [("Watchdog", "watchdog"), ("Process Manager", "process_manager")]:
                pid = None
                for p in psutil.process_iter(["pid", "name", "cmdline"]):
                    try:
                        cmd = " ".join(p.info.get("cmdline") or [])
                        if pattern in cmd:
                            pid = p.info["pid"]
                            break
                    except Exception:
                        continue
                if pid:
                    try:
                        proc = psutil.Process(pid)
                        cpu = proc.cpu_percent(interval=None)
                        mem = proc.memory_info().rss
                        create = datetime.fromtimestamp(proc.create_time()).astimezone().strftime("%H:%M")
                        lines.append(f"🟢 <b>{name}</b> — <code>RUNNING</code> PID:{pid} CPU:{cpu:.1f}% RSS:{_fmt_bytes(mem)} since {create}")
                    except Exception:
                        lines.append(f"🟢 <b>{name}</b> — <code>RUNNING</code> PID:{pid}")
                else:
                    lines.append(f"🔴 <b>{name}</b> — <code>NOT FOUND</code>")
        except Exception:
            pass
    lines.append("")

    # System resources
    lines.append("━━━━━━━━━━━━━━━━━━━")
    lines.append("<b>SYSTEM RESOURCES</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━")
    if resource_monitor:
        for rtype in [ResourceType.CPU, ResourceType.MEMORY, ResourceType.STORAGE, ResourceType.INODES]:
            metrics = resource_monitor.get_metrics(rtype)
            state = resource_monitor.get_state(rtype)
            emoji = _resource_emoji(state)
            if metrics:
                if rtype == ResourceType.CPU:
                    load = metrics.extra.get("load_avg", [0,0,0]) if metrics.extra else [0,0,0]
                    lines.append(f"{emoji} <b>CPU</b> — <code>{metrics.usage_percent:.1f}%</code> Load:{load[0]:.2f}/{metrics.extra.get('cpu_count',0) if metrics.extra else '?'}")
                elif rtype == ResourceType.MEMORY:
                    lines.append(f"{emoji} <b>RAM</b> — <code>{metrics.usage_percent:.1f}%</code> Used:{_fmt_bytes(metrics.used_bytes)} / {_fmt_bytes(metrics.total_bytes)} Avail:{_fmt_bytes(metrics.available_bytes)}")
                elif rtype == ResourceType.STORAGE:
                    mount = metrics.extra.get("mount", "/") if metrics.extra else "/"
                    lines.append(f"{emoji} <b>Storage {mount}</b> — <code>{metrics.usage_percent:.1f}%</code> Used:{_fmt_bytes(metrics.used_bytes)} / {_fmt_bytes(metrics.total_bytes)} Free:{_fmt_bytes(metrics.available_bytes)}")
                elif rtype == ResourceType.INODES:
                    lines.append(f"{emoji} <b>Inodes {metrics.extra.get('mount','/')}</b> — <code>{metrics.usage_percent:.1f}%</code> Used:{metrics.used_bytes} / {metrics.total_bytes}")
            else:
                lines.append(f"⚪ <b>{rtype.value}</b> — <code>unavailable</code>")
    else:
        # Fallback via psutil directly
        try:
            cpu = psutil.cpu_percent(interval=None)
            vmem = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            lines.append(f"🟢 <b>CPU</b> — <code>{cpu:.1f}%</code>")
            lines.append(f"🟢 <b>RAM</b> — <code>{vmem.percent:.1f}%</code> {_fmt_bytes(vmem.used)}/{_fmt_bytes(vmem.total)}")
            lines.append(f"🟢 <b>Storage /</b> — <code>{disk.percent:.1f}%</code> {_fmt_bytes(disk.used)}/{_fmt_bytes(disk.total)}")
        except Exception as e:
            lines.append(f"⚪ <b>Resources</b> — unavailable: {e}")

    lines.append("")

    # Trading summary
    lines.append("━━━━━━━━━━━━━━━━━━━")
    lines.append("<b>TRADING</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━")
    lines.append(f"Market: <code>{market_text}</code>")
    if is_open:
        lines.append("Execution: <code>ACTIVE</code> (within session)")
    else:
        lines.append("Execution: <code>NOT ACTIVE</code> (outside session)")
    # Gateway/Backend expected
    for svc in trading_services:
        snap = snapshots.get(svc)
        if snap:
            exp = "RUNNING" if is_open else "EXPECTED STOPPED"
            # If state is MARKET_CLOSED, expected
            if snap.state == ServiceState.MARKET_CLOSED:
                exp = "EXPECTED STOPPED"
            elif snap.state == ServiceState.HEALTHY and is_open:
                exp = "RUNNING"
            disp = svc.value
            lines.append(f"{disp}: <code>{exp}</code>")

    lines.append("")

    # Alerts
    lines.append("━━━━━━━━━━━━━━━━━━━")
    lines.append("<b>ALERTS</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━")
    has_alert = False
    for svc, snap in snapshots.items():
        if snap.state in (ServiceState.FAILED, ServiceState.MANUAL_INTERVENTION_REQUIRED, ServiceState.TRADING_BLOCKED):
            # But if market closed and svc is trading, MARKET_CLOSED is not alert
            if svc in trading_services and snap.state == ServiceState.MARKET_CLOSED:
                continue
            reason = _sanitize(snap.failure_reason[:60]) if snap.failure_reason else ""
            lines.append(f"🔴 <b>{svc.value}</b> — <code>{snap.state.value}</code> {reason}")
            has_alert = True
    if resource_monitor:
        for rtype in [ResourceType.CPU, ResourceType.MEMORY, ResourceType.STORAGE, ResourceType.INODES]:
            st = resource_monitor.get_state(rtype)
            if st in (ResourceState.WARNING, ResourceState.CRITICAL):
                m = resource_monitor.get_metrics(rtype)
                pct = m.usage_percent if m else 0
                emoji = "🔴" if st == ResourceState.CRITICAL else "🟠"
                lines.append(f"{emoji} <b>{rtype.value}</b> — <code>{st.value} {pct:.1f}%</code>")
                has_alert = True
    if not has_alert:
        lines.append("🟢 <code>No active infrastructure alerts</code>")

    # Host info footer
    try:
        hostname = socket.gethostname()
        uptime = time.time() - psutil.boot_time()
        days = int(uptime // 86400)
        hrs = int((uptime % 86400) // 3600)
        lines.append("")
        lines.append(f"<code>{hostname} up {days}d {hrs}h | {platform.system()} {platform.release()}</code>")
    except Exception:
        pass

    return "\n".join(lines)
