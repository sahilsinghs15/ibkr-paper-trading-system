"""CLI entrypoint for systemd ExecStartPost / ExecStopPost lifecycle notifications.

Allows systemd to record and dispatch stop/start notifications directly when services
are manually stopped or started via `systemctl` or when processes terminate.
"""

from __future__ import annotations

import asyncio
import logging
import sys
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
}


async def report_service_lifecycle(action: str, service: str) -> int:
    """Ingest service stop or start event and dispatch delivery."""
    cfg = SERVICE_MAP.get(service)
    if not cfg:
        logger.error("Unknown service '%s'. Allowed: %s", service, list(SERVICE_MAP.keys()))
        return 1

    is_start = action.lower() in ("start", "started", "up")
    svc_key = cfg["key"]

    components_override = {svc_key: {"ready": is_start}}
    if svc_key == "ib_gateway" and not is_start:
        components_override["broker_connection"] = {"ready": False}
        components_override["ib_login"] = {"ready": False}

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
            "action": action,
            "component": svc_key,
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
