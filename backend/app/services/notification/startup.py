"""Startup aggregator: aggregates individual service startup milestones into a single System Universe readiness decision."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models.notification import NotificationLogModel
from app.services.notification.orchestrator import NotificationOrchestrator
from app.services.notification.system_state import (
    CANONICAL_STARTUP_COMPONENTS,
    get_realtime_system_state,
)
from app.services.notification.types import (
    NormalizedEvent,
    NotificationSeverity,
    NotificationStatus,
)

logger = logging.getLogger(__name__)


class StartupAggregator:
    """Collects startup milestones and produces a consolidated System Universe READY or PARTIAL WARNING alert."""

    def __init__(
        self,
        orchestrator: NotificationOrchestrator,
        client: Any = None,
        *,
        window_sec: float | None = None,
        critical_components: tuple[str, ...] | None = None,
        component_labels: dict[str, str] | None = None,
        auto_probe: bool = True,
        session_id: str | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._client = client
        settings = get_settings()
        self._window_sec = window_sec or settings.notification_startup_window_sec
        self._session_id = session_id or uuid.uuid4().hex[:8]

        if critical_components is not None:
            self._critical_components = tuple(critical_components)
        else:
            self._critical_components = tuple(k for k, _ in CANONICAL_STARTUP_COMPONENTS)

        self._component_labels = dict(CANONICAL_STARTUP_COMPONENTS)
        if component_labels:
            self._component_labels.update(component_labels)

        self._auto_probe = auto_probe
        self._components: dict[str, dict[str, Any]] = {}
        self._sync_lock = threading.Lock()
        self._lock = asyncio.Lock()
        self._published = False
        self._is_active = False
        self._aggregation_task: asyncio.Task[None] | None = None

    def set_client(self, client: Any) -> None:
        """Set or update the TWSClient reference for dynamic state checks."""
        self._client = client

    @property
    def is_window_active(self) -> bool:
        """True if startup aggregation window is currently collecting milestones."""
        with self._sync_lock:
            return self._is_active and not self._published

    @property
    def has_published(self) -> bool:
        """True if the aggregate startup decision has already been published."""
        with self._sync_lock:
            return self._published

    def record_component(
        self,
        name: str,
        is_ready: bool,
        detail: str = "",
    ) -> None:
        """Record status of an individual startup subsystem."""
        with self._sync_lock:
            if self._published:
                logger.warning(
                    "Startup component '%s' recorded after window closed: ready=%s detail='%s'",
                    name,
                    is_ready,
                    detail,
                )
            self._components[name] = {
                "ready": is_ready,
                "detail": detail,
            }
        logger.info(
            "Startup component '%s' recorded: ready=%s detail='%s'",
            name,
            is_ready,
            detail,
        )

    async def start_window(self) -> None:
        """Start the background timer and auto-probe for aggregation window."""
        with self._sync_lock:
            if self._aggregation_task is not None or self._published:
                return
            self._is_active = True
        self._aggregation_task = asyncio.create_task(
            self._timer_loop(), name="startup-aggregator-timer"
        )

    async def _probe_external_components(self) -> None:
        """Probe localhost HTTP health endpoints for external services if needed."""
        # 1. Host environment
        if "ec2_instance" in self._critical_components and "ec2_instance" not in self._components:
            try:
                import os
                load = os.getloadavg()
                if len(load) == 3 and load[0] >= 0:
                    self.record_component("ec2_instance", is_ready=True, detail="Host environment nominal")
            except OSError:
                pass

        # 2. Signal receiver (webhook ingest)
        if "signal_receiver" in self._critical_components and "signal_receiver" not in self._components:
            try:
                async with httpx.AsyncClient(timeout=1.0) as client:
                    res = await client.get("http://127.0.0.1:8000/health")
                    if res.status_code == 200:
                        self.record_component("signal_receiver", is_ready=True, detail="HTTP 200")
            except (httpx.HTTPError, OSError) as exc:
                logger.debug("Signal receiver probe failed: %s", exc)

        # 3. Dashboard engine (demo streaming)
        if "dashboard_engine" in self._critical_components and "dashboard_engine" not in self._components:
            try:
                async with httpx.AsyncClient(timeout=1.0) as client:
                    res = await client.get("http://127.0.0.1:8010/health")
                    if res.status_code == 200:
                        self.record_component("dashboard_engine", is_ready=True, detail="HTTP 200")
            except (httpx.HTTPError, OSError) as exc:
                logger.debug("Dashboard engine probe failed: %s", exc)

        # 4. OEMS engine (trading backend)
        if "oems_engine" in self._critical_components and "oems_engine" not in self._components:
            try:
                async with httpx.AsyncClient(timeout=1.0) as client:
                    res = await client.get("http://127.0.0.1:8001/health")
                    if res.status_code == 200:
                        self.record_component("oems_engine", is_ready=True, detail="HTTP 200")
            except (httpx.HTTPError, OSError) as exc:
                logger.debug("OEMS engine probe failed: %s", exc)

        # 4. IB Gateway process
        if "ib_gateway" in self._critical_components and "ib_gateway" not in self._components:
            is_gateway_active = False
            try:
                proc = await asyncio.create_subprocess_exec(
                    "systemctl", "is-active", "--quiet", "ibgateway",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
                if proc.returncode == 0:
                    is_gateway_active = True
            except (OSError, subprocess.SubprocessError) as exc:
                logger.debug("IB Gateway systemctl check failed: %s", exc)

            if not is_gateway_active:
                try:
                    import psutil  # type: ignore
                    for p in psutil.process_iter(["name", "cmdline"]):
                        cmd = " ".join(p.info.get("cmdline") or [])
                        name = p.info.get("name") or ""
                        if "ibgateway" in cmd or "ibcstart" in cmd or "Xvfb" in cmd or "Xvfb" in name:
                            is_gateway_active = True
                            break
                except (ImportError, OSError) as exc:
                    logger.debug("IB Gateway psutil check failed: %s", exc)

            if is_gateway_active:
                self.record_component("ib_gateway", is_ready=True, detail="Process active (systemd/IBC)")

    async def _timer_loop(self) -> None:
        """Wait for window to elapse or early exit if all components ready, then publish."""
        try:
            start_time = asyncio.get_running_loop().time()
            deadline = start_time + self._window_sec
            min_settle_time = start_time + min(6.0, self._window_sec * 0.8)

            while asyncio.get_running_loop().time() < deadline:
                if self._auto_probe:
                    await self._probe_external_components()

                # Check if all critical components are already recorded and ready
                async with self._lock:
                    crit_set = set(self._critical_components)
                    if crit_set.issubset(self._components.keys()):
                        all_ready = all(
                            self._components[c]["ready"]
                            for c in self._critical_components
                            if c in self._components
                        )
                        if all_ready and asyncio.get_running_loop().time() >= min_settle_time:
                            logger.info("All critical startup components ready and settled early; concluding window")
                            break
                await asyncio.sleep(min(0.2, self._window_sec))

            await self.publish_readiness()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("StartupAggregator encountered unexpected error in timer loop")

    async def _has_unrecovered_broker_lost(self) -> bool:
        """Check if this startup is resolving an active, unrecovered BROKER_LOST incident."""
        try:
            trigger_path = Path("/home/tradingapp/storage/state/restart_backend.trigger")
            if trigger_path.exists():
                try:
                    trigger_path.unlink(missing_ok=True)
                except OSError:
                    pass

            session_factory = getattr(self._orchestrator, "_session_factory", None)
            if session_factory is not None:
                async with session_factory() as session:
                    stmt = (
                        select(NotificationLogModel)
                        .where(
                            NotificationLogModel.category == "BROKER",
                            NotificationLogModel.status != NotificationStatus.SUPPRESSED.value,
                        )
                        .order_by(NotificationLogModel.id.desc())
                        .limit(1)
                    )
                    last_broker_event = (await session.execute(stmt)).scalar_one_or_none()
                    if last_broker_event is not None and last_broker_event.event_type == "BROKER_LOST":
                        logger.info(
                            "Found open BROKER_LOST incident (%s) in notification_log; treating startup as broker recovery",
                            last_broker_event.notification_id,
                        )
                        return True
        except Exception:
            logger.exception("Failed checking for unrecovered broker incident")
        return False

    async def publish_readiness(self) -> None:
        """Evaluate recorded components and publish unified aggregate notification."""
        async with self._lock:
            with self._sync_lock:
                if self._published:
                    return
                self._published = True
                self._is_active = False

            states, status_summary = await get_realtime_system_state(
                client=self._client,
                components=self._components,
                probe_endpoints=self._auto_probe,
            )

            for k, ready in states.items():
                if k not in self._components:
                    self._components[k] = {
                        "ready": ready,
                        "detail": "probed" if self._auto_probe else "",
                    }

            missing_critical = set(self._critical_components) - set(self._components.keys())
            failed_components = [
                name
                for name, info in self._components.items()
                if not info.get("ready", False) and name in self._critical_components
            ]

            is_fully_ready = (not missing_critical) and (not failed_components)

            # Check if this startup resolves an active unrecovered BROKER_LOST incident
            is_broker_recovery = await self._has_unrecovered_broker_lost()

            if is_broker_recovery:
                title = "🟢 IBKR Broker Connected Successfully"
                event_type = "BROKER_RECONNECTED"
                category = "BROKER"
                correlation_id = "broker_connection"
                severity = NotificationSeverity.INFO if is_fully_ready else NotificationSeverity.WARNING
            elif is_fully_ready:
                title = "🟢 System Universe Started Successfully"
                event_type = "STARTUP_AGGREGATION"
                category = "SYSTEM"
                correlation_id = f"system_universe_startup_{self._session_id}"
                severity = NotificationSeverity.INFO
            else:
                title = "⚠️ System Universe Startup Warning"
                event_type = "STARTUP_AGGREGATION"
                category = "SYSTEM"
                correlation_id = f"system_universe_startup_{self._session_id}"
                severity = NotificationSeverity.WARNING

            event = NormalizedEvent(
                event_type=event_type,
                title=title,
                message=status_summary,
                category=category,
                severity=severity,
                dedupe_key=f"{event_type.lower()}_{self._session_id}",
                correlation_id=correlation_id,
                details={
                    "components": self._components,
                    "ready": is_fully_ready,
                    "failed": failed_components,
                    "missing": list(missing_critical),
                    "is_broker_recovery": is_broker_recovery,
                },
            )

            await self._orchestrator.ingest_event(event)
            logger.info("Published aggregate notification: event_type=%s ready=%s title='%s'", event_type, is_fully_ready, title)

    async def stop(self) -> None:
        """Cancel aggregation task if active."""
        if self._aggregation_task is not None:
            self._aggregation_task.cancel()
            try:
                await self._aggregation_task
            except asyncio.CancelledError:
                pass
