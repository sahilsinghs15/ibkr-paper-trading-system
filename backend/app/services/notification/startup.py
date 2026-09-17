"""Startup aggregator: aggregates individual service startup milestones into a single OEMS readiness decision."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from app.core.config import get_settings
from app.services.notification.orchestrator import NotificationOrchestrator
from app.services.notification.types import (
    NormalizedEvent,
    NotificationSeverity,
)

logger = logging.getLogger(__name__)


class StartupAggregator:
    """Collects startup milestones and produces a consolidated OEMS READY or PARTIAL WARNING alert."""

    def __init__(
        self,
        orchestrator: NotificationOrchestrator,
        *,
        window_sec: float | None = None,
        critical_components: tuple[str, ...] = (
            "database",
            "broker",
            "worker_pool",
            "position_reconciler",
        ),
    ) -> None:
        self._orchestrator = orchestrator
        settings = get_settings()
        self._window_sec = window_sec or settings.notification_startup_window_sec
        self._critical_components = set(critical_components)
        self._components: dict[str, dict[str, Any]] = {}
        self._sync_lock = threading.Lock()
        self._lock = asyncio.Lock()
        self._published = False
        self._aggregation_task: asyncio.Task[None] | None = None

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
        """Start the background timer for aggregation window."""
        if self._aggregation_task is not None:
            return
        self._aggregation_task = asyncio.create_task(
            self._timer_loop(), name="startup-aggregator-timer"
        )

    async def _timer_loop(self) -> None:
        """Wait for window to elapse or early exit if all components ready, then publish."""
        try:
            start_time = asyncio.get_running_loop().time()
            deadline = start_time + self._window_sec

            while asyncio.get_running_loop().time() < deadline:
                # Check if all critical components are already recorded and ready
                async with self._lock:
                    if self._critical_components.issubset(self._components.keys()):
                        all_ready = all(
                            self._components[c]["ready"]
                            for c in self._critical_components
                            if c in self._components
                        )
                        if all_ready:
                            logger.info("All critical components ready early; concluding startup window")
                            break
                await asyncio.sleep(1.0)

            await self.publish_readiness()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("StartupAggregator encountered unexpected error in timer loop")

    async def publish_readiness(self) -> None:
        """Evaluate recorded components and publish unified aggregate notification."""
        async with self._lock:
            if self._published:
                return
            self._published = True

            missing_critical = self._critical_components - set(self._components.keys())
            failed_components = [
                name
                for name, info in self._components.items()
                if not info["ready"]
            ]

            is_fully_ready = (not missing_critical) and (not failed_components)

            # Build summary message
            component_lines = []
            for name, info in sorted(self._components.items()):
                icon = "🟢" if info["ready"] else "🔴"
                detail = f" — {info['detail']}" if info["detail"] else ""
                component_lines.append(f"{icon} {name.replace('_', ' ').title()}: {'Ready' if info['ready'] else 'Failed'}{detail}")

            for missing in sorted(missing_critical):
                component_lines.append(f"🔴 {missing.replace('_', ' ').title()}: Not Started / Missing")

            status_summary = "\n".join(component_lines)

            if is_fully_ready:
                event = NormalizedEvent(
                    event_type="STARTUP_AGGREGATION",
                    title="GLOBAL OEMS READY",
                    message=f"All critical systems nominal.\n{status_summary}",
                    category="SYSTEM",
                    severity=NotificationSeverity.INFO,
                    dedupe_key="startup_aggregation_session",
                    details={"components": self._components, "ready": True},
                )
            else:
                event = NormalizedEvent(
                    event_type="STARTUP_AGGREGATION",
                    title="PARTIAL STARTUP WARNING",
                    message=f"One or more subsystems degraded at startup.\n{status_summary}",
                    category="SYSTEM",
                    severity=NotificationSeverity.WARNING,
                    dedupe_key="startup_aggregation_session",
                    details={
                        "components": self._components,
                        "ready": False,
                        "failed": failed_components,
                        "missing": list(missing_critical),
                    },
                )

            await self._orchestrator.ingest_event(event)
            logger.info("Published aggregate startup notification: ready=%s", is_fully_ready)

    async def stop(self) -> None:
        """Cancel aggregation task if active."""
        if self._aggregation_task is not None:
            self._aggregation_task.cancel()
            try:
                await self._aggregation_task
            except asyncio.CancelledError:
                pass
