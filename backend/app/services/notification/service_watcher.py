"""Service lifecycle watcher: observes independently observable external services (demo-streaming, signal-receiver).

Emits stop/recovery notifications with real-time System Universe state snapshots.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.services.notification.orchestrator import NotificationOrchestrator
from app.services.notification.system_state import get_realtime_system_state
from app.services.notification.types import (
    NormalizedEvent,
    NotificationSeverity,
)

logger = logging.getLogger(__name__)

WATCHED_SERVICES = {
    "dashboard_engine": {
        "url": "http://127.0.0.1:8010/health",
        "service_name": "demo-streaming",
        "stop_title": "🔴 Dashboard Engine Stopped",
        "start_title": "🟢 Dashboard Engine Started Successfully",
    },
    "signal_receiver": {
        "url": "http://127.0.0.1:8000/health",
        "service_name": "webhook-ingest",
        "stop_title": "🔴 Signal Receiver Stopped",
        "start_title": "🟢 Signal Receiver Started Successfully",
    },
}


class ServiceLifecycleWatcher:
    """Monitors external services via localhost health probes and reports state transitions."""

    def __init__(
        self,
        orchestrator: NotificationOrchestrator,
        client: Any = None,
        *,
        poll_interval_sec: float = 2.0,
    ) -> None:
        self._orchestrator = orchestrator
        self._client = client
        self._poll_interval = poll_interval_sec
        # Service states: None = initial unknown, True = ready, False = stopped
        self._states: dict[str, bool | None] = {svc: None for svc in WATCHED_SERVICES}
        self._task: asyncio.Task[None] | None = None
        self._running = False

    def set_client(self, client: Any) -> None:
        self._client = client

    async def start(self) -> None:
        """Start the background polling loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name="service-lifecycle-watcher")

    async def stop(self) -> None:
        """Stop the background polling loop."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _probe(self, url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=1.0) as http_client:
                res = await http_client.get(url)
                return res.status_code == 200
        except (httpx.HTTPError, OSError):
            return False

    async def _poll_loop(self) -> None:
        # Give services 5 seconds settle time after backend boot before triggering alerts
        await asyncio.sleep(5.0)

        # Initialize initial state without emitting events
        for svc, cfg in WATCHED_SERVICES.items():
            self._states[svc] = await self._probe(cfg["url"])

        while self._running:
            try:
                for svc, cfg in WATCHED_SERVICES.items():
                    current_up = await self._probe(cfg["url"])
                    prev_up = self._states[svc]

                    if prev_up is not None and prev_up != current_up:
                        self._states[svc] = current_up
                        await self._handle_transition(svc, cfg, current_up)

                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Unexpected error in ServiceLifecycleWatcher loop")
                await asyncio.sleep(self._poll_interval)

    async def _handle_transition(
        self,
        svc_key: str,
        cfg: dict[str, str],
        is_up: bool,
    ) -> None:
        """Handle a detected transition and emit the corresponding lifecycle notification."""
        components_override = {svc_key: {"ready": is_up}}
        _, state_table = await get_realtime_system_state(
            client=self._client,
            components=components_override,
        )

        if not is_up:
            event_type = "SERVICE_STOPPED"
            title = cfg["stop_title"]
            severity = NotificationSeverity.CRITICAL
        else:
            event_type = "SERVICE_STARTED"
            title = cfg["start_title"]
            severity = NotificationSeverity.INFO

        event = NormalizedEvent(
            event_type=event_type,
            title=title,
            message=state_table,
            category="SYSTEM",
            severity=severity,
            correlation_id=svc_key,
            details={
                "service": cfg["service_name"],
                "component": svc_key,
                "ready": is_up,
            },
        )
        try:
            logger.info("ServiceLifecycleWatcher emitting %s for %s", event_type, svc_key)
            await self._orchestrator.ingest_event(event)
        except Exception:
            logger.exception("Failed ingesting %s event for %s", event_type, svc_key)
