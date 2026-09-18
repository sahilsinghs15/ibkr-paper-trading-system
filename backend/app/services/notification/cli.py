"""CLI entrypoint for systemd ExecStartPost / ExecStopPost lifecycle notifications.

Allows systemd to record and dispatch stop/start notifications directly when services
are manually stopped or started via `systemctl` or when processes terminate.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from app.db.session import AsyncSessionLocal
from app.services.notification.dispatcher import get_default_dispatcher
from app.services.notification.orchestrator import NotificationOrchestrator
from app.services.notification.system_state import get_realtime_system_state
from app.services.notification.types import (
    NormalizedEvent,
    NotificationSeverity,
)
from app.services.notification.worker import NotificationDeliveryWorker

logger = logging.getLogger("notification_cli")

SERVICE_MAP: dict[str, dict[str, Any]] = {
    "trading-backend": {
        "key": "oems_engine",
        "stop_title": "🔴 OEMS Engine Stopped",
        "start_title": "🟢 OEMS Engine Started Successfully",
        "name": "OEMS Engine",
    },
    "ibgateway": {
        "key": "ib_gateway",
        "stop_title": "🔴 IB Gateway Stopped",
        "start_title": "🟢 IB Gateway Started Successfully",
        "name": "IB Gateway",
    },
    "demo-streaming": {
        "key": "dashboard_engine",
        "stop_title": "🔴 Dashboard Engine Stopped",
        "start_title": "🟢 Dashboard Engine Started Successfully",
        "name": "Dashboard Engine",
    },
    "webhook-ingest": {
        "key": "signal_receiver",
        "stop_title": "🔴 Signal Receiver Stopped",
        "start_title": "🟢 Signal Receiver Started Successfully",
        "name": "Signal Receiver",
    },
    "server-machine": {
        "key": "ec2_instance",
        "stop_title": "🔴 Server Machine Stopped",
        "start_title": "🟢 Server Machine Started Successfully",
        "name": "Server Machine",
    },
    "ec2-instance": {
        "key": "ec2_instance",
        "stop_title": "🔴 Server Machine Stopped",
        "start_title": "🟢 Server Machine Started Successfully",
        "name": "Server Machine",
    },
}


async def report_service_lifecycle(action: str, service: str) -> int:
    """Ingest service stop or start event and dispatch delivery."""
    cfg = SERVICE_MAP.get(service)
    if not cfg:
        logger.error("Unknown service '%s'. Allowed: %s", service, list(SERVICE_MAP.keys()))
        return 1

    is_start = action.lower() in ("start", "started", "up")
    svc_key = cfg["key"]

    # For stop actions: verify whether this is an instantaneous restart
    # (systemd restart stops then immediately re-activates the unit).
    # We poll for up to 10s to accommodate uvicorn/FastAPI startup time (~5s).
    # NOTE: ec2_instance shutdown must NOT wait; it must dispatch immediately before OS cuts network!
    if not is_start and svc_key != "ec2_instance":
        auto_restart_flag = Path("/home/tradingapp/storage/state/backend_auto_restarting.flag")
        if auto_restart_flag.exists():
            logger.info("Auto-restart flag active for %s; suppressing stop alert", service)
            return 0

        is_restarting = False
        # 1. Immediate check: does systemd have a 'restart' job active or queued for this unit?
        try:
            proc_jobs = await asyncio.create_subprocess_exec(
                "systemctl", "list-jobs",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout_jobs, _ = await proc_jobs.communicate()
            for line in stdout_jobs.decode().splitlines():
                if service in line and "restart" in line:
                    is_restarting = True
                    break
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("Failed checking list-jobs for %s: %s", service, exc)

        # 2. Polling check: wait for reactivation if restart is transitioning
        if not is_restarting:
            for _ in range(10):
                await asyncio.sleep(1.0)
                try:
                    # Check list-jobs again
                    proc_jobs = await asyncio.create_subprocess_exec(
                        "systemctl", "list-jobs",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    stdout_jobs, _ = await proc_jobs.communicate()
                    for line in stdout_jobs.decode().splitlines():
                        if service in line and "restart" in line:
                            is_restarting = True
                            break
                    if is_restarting:
                        break

                    # Check is-active
                    proc = await asyncio.create_subprocess_exec(
                        "systemctl", "is-active", service,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    stdout, _ = await proc.communicate()
                    state_str = stdout.decode().strip().lower()
                    if state_str in ("active", "activating"):
                        is_restarting = True
                        break
                except (OSError, subprocess.SubprocessError) as exc:
                    logger.debug("Failed checking status for %s: %s", service, exc)

        if is_restarting:
            logger.info(
                "Service '%s' restart in progress or completed; suppressing stop alert",
                service,
            )
            return 0

    components_override = {svc_key: {"ready": is_start}}
    if svc_key == "ec2_instance" and not is_start:
        components_override = {
            "ec2_instance": {"ready": False},
            "ib_gateway": {"ready": False},
            "ib_login": {"ready": False},
            "broker_connection": {"ready": False},
            "signal_receiver": {"ready": False},
            "oems_engine": {"ready": False},
            "dashboard_engine": {"ready": False},
        }
    elif svc_key == "ib_gateway" and not is_start:
        components_override["broker_connection"] = {"ready": False}
        components_override["ib_login"] = {"ready": False}
    elif svc_key == "oems_engine" and not is_start:
        components_override["broker_connection"] = {"ready": False}

    title = ""
    state_table = ""
    try:
        _, state_table = await get_realtime_system_state(
            components=components_override,
            probe_endpoints=True,
        )

        if is_start:
            event_type = "SERVICE_STARTED"
            title = cfg["start_title"]
            severity = NotificationSeverity.INFO
        else:
            event_type = "SERVICE_STOPPED"
            title = cfg["stop_title"]
            severity = NotificationSeverity.CRITICAL

        event = NormalizedEvent(
            event_type=event_type,
            title=title,
            message=state_table,
            category="SYSTEM",
            severity=severity,
            correlation_id=svc_key,
            details={
                "service": service,
                "component": svc_key,
                "ready": is_start,
                "action": action,
            },
        )

        orchestrator = NotificationOrchestrator(AsyncSessionLocal)
        notif = await orchestrator.ingest_event(event)

        # Immediately attempt delivery for fast operator feedback
        if notif is not None and notif.status != "SUPPRESSED":
            worker = NotificationDeliveryWorker(
                session_factory=AsyncSessionLocal,
                dispatcher=get_default_dispatcher(),
            )
            await worker.run_once()
    except Exception:
        logger.exception("Failed to report lifecycle event for %s %s via DB orchestrator", action, service)
        # Direct fallback for emergency shutdown if DB is unavailable
        try:
            from app.core.config import get_settings
            settings = get_settings()
            token = settings.telegram_bot_token
            chat_id = settings.telegram_chat_id
            if settings.telegram_enabled and token and chat_id and title and state_table:
                import httpx
                url = f"https://api.telegram.org/bot{token}/sendMessage"
                text = f"<b>{title}</b>\n\n{state_table}"
                async with httpx.AsyncClient(timeout=4.0) as http_client:
                    await http_client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
        except Exception:
            logger.exception("Direct Telegram fallback also failed for %s %s", action, service)

    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    if len(args) < 2:
        print("Usage: python -m app.services.notification.cli <start|stop> <service_name>", file=sys.stderr)
        return 1

    action, service = args[0].lower(), args[1].lower()
    return asyncio.run(report_service_lifecycle(action, service))


if __name__ == "__main__":
    sys.exit(main())
