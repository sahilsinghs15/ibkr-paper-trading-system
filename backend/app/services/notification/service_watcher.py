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

# A health probe that does not answer in time is not evidence of a stop, so a
# service must fail this many consecutive probes before it is reported down.
DOWN_CONFIRM_PROBES = 3
# Generous enough to survive a GC pause or a burst of SSE traffic on the
# dashboard; the previous 1s timed out under ordinary load.
PROBE_TIMEOUT_SEC = 3.0

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
        probe_timeout_sec: float = PROBE_TIMEOUT_SEC,
        failure_threshold: int = DOWN_CONFIRM_PROBES,
    ) -> None:
        self._orchestrator = orchestrator
        self._client = client
        self._poll_interval = poll_interval_sec
        self._probe_timeout = probe_timeout_sec
        self._failure_threshold = max(1, failure_threshold)
        # Service states: None = initial unknown, True = ready, False = stopped
        self._states: dict[str, bool | None] = {svc: None for svc in WATCHED_SERVICES}
        # Consecutive failed probes per service. A single failure means "no answer",
        # not "stopped"; only a run of them is evidence the service is down.
        self._consecutive_failures: dict[str, int] = {svc: 0 for svc in WATCHED_SERVICES}
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
            async with httpx.AsyncClient(timeout=self._probe_timeout) as http_client:
                res = await http_client.get(url)
                return res.status_code == 200
        except (httpx.HTTPError, OSError):
            return False

    def _observe(self, svc: str, probe_ok: bool) -> bool | None:
        """Fold one probe result into a confirmed state, or None while undecided.

        Going *down* needs `failure_threshold` consecutive failures: a probe that
        times out proves only that no answer arrived within the timeout, which is
        routine under load. Treating the first failure as a stop produced
        "Dashboard Engine Stopped" immediately followed by "Started" two seconds
        later, repeatedly, until the flapping detector throttled the subsystem --
        for a service systemd never restarted.

        Coming *up* is confirmed by a single success, because a 200 response is
        positive evidence rather than the absence of one.
        """
        if probe_ok:
            self._consecutive_failures[svc] = 0
            return True
        self._consecutive_failures[svc] += 1
        if self._consecutive_failures[svc] >= self._failure_threshold:
            return False
        return None

    async def _poll_loop(self) -> None:
        # Give services 5 seconds settle time after backend boot before triggering alerts
        await asyncio.sleep(5.0)

        # Initialize initial state without emitting events
        for svc, cfg in WATCHED_SERVICES.items():
            self._states[svc] = await self._probe(cfg["url"])

        while self._running:
            try:
                for svc, cfg in WATCHED_SERVICES.items():
                    confirmed = self._observe(svc, await self._probe(cfg["url"]))
                    if confirmed is None:
                        continue  # not enough evidence yet; say nothing
                    prev_up = self._states[svc]

                    if prev_up is not None and prev_up != confirmed:
                        self._states[svc] = confirmed
                        await self._handle_transition(svc, cfg, confirmed)

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
