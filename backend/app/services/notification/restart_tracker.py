"""Durable record of service restarts, bridging the stop hook and the startup aggregator.

A systemd restart stops a unit and immediately re-activates it. `cli.py` detects
that and suppresses the stop alert, which is correct — an operator does not want
"OEMS Engine Stopped" for a restart. Until this module existed the restart fact
was then discarded, so the startup that followed looked identical to a cold boot
and reported "System Universe Started Successfully". That left the operator with
a message and no way to tell why it arrived or what had actually changed.

The stop hook now records a marker; the startup aggregator consumes it and can
report the restart truthfully instead.

Markers are files so they survive the process dying between the stop hook and the
next start — the two run in different processes, and the restarting one is the
process that would otherwise have held the state in memory.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_STATE_DIR = Path("/home/tradingapp/storage/state")
_MARKER_SUFFIX = ".restart.json"

# How long a marker stays relevant. A restart that does not produce a startup
# within this window is stale — the service failed to come back, and the startup
# aggregation that eventually runs belongs to a different event.
DEFAULT_TTL_SEC = 300.0

# Service -> the component key it maps to in CANONICAL_STARTUP_COMPONENTS.
# Mirrors SERVICE_MAP in cli.py; kept here because cli imports this module.
SERVICE_COMPONENTS: dict[str, str] = {
    "trading-backend": "oems_engine",
    "ibgateway": "ib_gateway",
    "demo-streaming": "dashboard_engine",
    "webhook-ingest": "signal_receiver",
    "server-machine": "ec2_instance",
    "ec2-instance": "ec2_instance",
}

# Restarts systemd performs as a consequence of another unit restarting. Each
# edge is defined entirely in the unit files and is one-way:
#
#   ibgateway -> trading-backend
#     scripts/ibgateway-wrapper.sh touches restart_backend.trigger once the
#     gateway reports ready; trading-backend-restart.path watches that file.
#
#   trading-backend -> demo-streaming
#     trading-backend.service ExecStartPost runs scripts/backend-ready-trigger.sh,
#     which touches restart_demo.trigger once the backend is healthy;
#     demo-streaming-restart.path watches that file.
#
# Chaining these gives ibgateway -> trading-backend -> demo-streaming.
#
# server-machine is deliberately absent: a host reboot starts everything from
# cold, and "System Universe Started" is the honest description of that. It is
# not one service restarting and dragging others with it.
CASCADE_MAP: dict[str, tuple[str, ...]] = {
    "ibgateway": ("trading-backend",),
    "trading-backend": ("demo-streaming",),
}


@dataclass(frozen=True)
class RestartRecord:
    """One recorded service restart."""

    service: str
    component: str
    recorded_at: float

    @property
    def age_sec(self) -> float:
        return max(0.0, time.time() - self.recorded_at)


def state_dir() -> Path:
    """Marker directory. Overridable so tests and dev machines do not need /home/tradingapp."""
    override = os.environ.get("TRADINGAPP_STATE_DIR")
    return Path(override) if override else DEFAULT_STATE_DIR


def record_restart(service: str, component: str) -> None:
    """Record that `service` is restarting. Best-effort: never raises.

    Called from the systemd stop hook, which must not fail the unit stop.
    """
    try:
        target = state_dir()
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{service}{_MARKER_SUFFIX}"
        payload = {
            "service": service,
            "component": component,
            "recorded_at": time.time(),
        }
        path.write_text(json.dumps(payload))
        logger.info("Recorded restart marker for %s at %s", service, path)
    except OSError as exc:
        logger.warning("Could not record restart marker for %s: %s", service, exc)


def consume_restarts(ttl_sec: float = DEFAULT_TTL_SEC) -> list[RestartRecord]:
    """Read and delete all restart markers, dropping any older than `ttl_sec`.

    Consuming is destructive on purpose: a restart should be reported once. If
    the report fails the marker is already gone, which is the right trade — a
    missed restart line is better than every subsequent startup claiming to be
    a restart.
    """
    records: list[RestartRecord] = []
    try:
        target = state_dir()
        if not target.is_dir():
            return records
        for path in sorted(target.glob(f"*{_MARKER_SUFFIX}")):
            try:
                data = json.loads(path.read_text())
                record = RestartRecord(
                    service=str(data["service"]),
                    component=str(data["component"]),
                    recorded_at=float(data["recorded_at"]),
                )
            except (OSError, ValueError, KeyError, TypeError) as exc:
                logger.warning("Discarding unreadable restart marker %s: %s", path, exc)
                _unlink(path)
                continue
            _unlink(path)
            if record.age_sec > ttl_sec:
                logger.info(
                    "Discarding stale restart marker for %s (age %.0fs > ttl %.0fs)",
                    record.service,
                    record.age_sec,
                    ttl_sec,
                )
                continue
            records.append(record)
    except OSError as exc:
        logger.warning("Could not read restart markers: %s", exc)
    return records


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not remove restart marker %s", path)


def cascaded_from(service: str, restarted: set[str]) -> str | None:
    """Return the service whose restart caused `service` to restart, if any."""
    for primary, followers in CASCADE_MAP.items():
        if service in followers and primary in restarted:
            return primary
    return None


@dataclass(frozen=True)
class RestartNode:
    """One service in a restart chain, with what caused it and how deep it sits."""

    service: str
    component: str
    caused_by: str | None
    depth: int
    observed: bool
    """True when a marker was recorded for this service.

    Cascaded units are reported even without a marker: demo-streaming.service has
    no ExecStopPost hook, so it can never record one, yet restarting
    trading-backend always restarts it. The chain is a property of the unit
    files, not something we need evidence of. Whether the service actually came
    back is a separate question, answered by the probed Current State block.
    """


def expand_restart_chain(restarts: list[RestartRecord]) -> list[RestartNode]:
    """Expand observed restarts into the full ordered chain they imply.

    Returns roots first, each immediately followed by what it triggered, depth
    first, so the rendered list reads top-down as cause -> effect.
    """
    observed = {r.service: r for r in restarts}

    # A root is an observed restart that nothing else observed caused.
    roots = [r.service for r in restarts if cascaded_from(r.service, set(observed)) is None]

    nodes: list[RestartNode] = []
    seen: set[str] = set()

    def walk(service: str, caused_by: str | None, depth: int) -> None:
        if service in seen:
            return
        seen.add(service)
        record = observed.get(service)
        component = (
            record.component
            if record is not None
            else SERVICE_COMPONENTS.get(service, service.replace("-", "_"))
        )
        nodes.append(
            RestartNode(
                service=service,
                component=component,
                caused_by=caused_by,
                depth=depth,
                observed=record is not None,
            )
        )
        for follower in CASCADE_MAP.get(service, ()):
            walk(follower, service, depth + 1)

    for root in roots:
        walk(root, None, 0)

    # Any observed restart not reachable from a root is its own root (for
    # example webhook-ingest restarted at the same time as something else).
    for record in restarts:
        if record.service not in seen:
            walk(record.service, None, 0)

    return nodes
