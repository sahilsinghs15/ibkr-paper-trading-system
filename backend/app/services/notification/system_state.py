"""Real-time system state snapshot for lifecycle and recovery notifications."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from typing import Any

import httpx

logger = logging.getLogger(__name__)

CANONICAL_STARTUP_COMPONENTS = (
    ("ec2_instance", "Server Machine"),
    ("ib_gateway", "IB Gateway"),
    ("ib_login", "IB Login"),
    ("broker_connection", "Broker Connection"),
    ("signal_receiver", "Signal Receiver"),
    ("oems_engine", "OEMS Engine"),
    ("dashboard_engine", "Dashboard Engine"),
)


async def get_realtime_system_state(
    client: Any = None,
    components: dict[str, Any] | None = None,
    *,
    probe_endpoints: bool | None = None,
) -> tuple[dict[str, bool], str]:
    """Inspect actual real-time state across all canonical subsystems.

    Returns (states_dict, pre_formatted_table).
    """
    if probe_endpoints is None:
        probe_endpoints = os.environ.get("TRADINGAPP_TESTING") != "1"

    states: dict[str, bool] = {}
    components = components or {}

    # 1. Server Machine (Host environment)
    if probe_endpoints:
        is_server_healthy = False
        try:
            load = os.getloadavg()
            is_server_healthy = len(load) == 3 and load[0] >= 0
        except OSError:
            pass
        states["ec2_instance"] = is_server_healthy
    else:
        states["ec2_instance"] = bool(components.get("ec2_instance", {}).get("ready", False))

    # 2. IB Gateway (systemd ibgateway.service / process check)
    if probe_endpoints:
        is_gw_active = False
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "is-active", "--quiet", "ibgateway",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode == 0:
                is_gw_active = True
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("Systemctl ibgateway check: %s", exc)

        if not is_gw_active:
            try:
                import psutil  # type: ignore
                for p in psutil.process_iter(["name", "cmdline"]):
                    cmd = " ".join(p.info.get("cmdline") or [])
                    name = p.info.get("name") or ""
                    if "ibgateway" in cmd or "ibcstart" in cmd or "Xvfb" in cmd or "Xvfb" in name:
                        is_gw_active = True
                        break
            except (ImportError, OSError) as exc:
                logger.debug("psutil ibgateway check: %s", exc)

        states["ib_gateway"] = is_gw_active
    else:
        states["ib_gateway"] = bool(components.get("ib_gateway", {}).get("ready", False))

    # 3. IB Login (authoritative nextValidId / authenticated handshake)
    if "ib_login" in components:
        states["ib_login"] = bool(components["ib_login"].get("ready", False))
    elif client is not None and hasattr(client, "is_connected"):
        try:
            connected = client.is_connected()
            order_id = getattr(client, "next_order_id", None)
            states["ib_login"] = bool(bool(connected) and order_id is not None)
        except (AttributeError, TypeError, RuntimeError, OSError):
            states["ib_login"] = False
    else:
        states["ib_login"] = False

    # 4. Broker Connection (TWSClient socket session)
    if "broker_connection" in components:
        states["broker_connection"] = bool(components["broker_connection"].get("ready", False))
    elif client is not None and hasattr(client, "is_connected"):
        try:
            connected = client.is_connected()
            states["broker_connection"] = bool(connected)
        except (AttributeError, TypeError, RuntimeError, OSError):
            states["broker_connection"] = False
    else:
        states["broker_connection"] = False

    # 5. Signal Receiver (HTTP :8000/health)
    if probe_endpoints:
        is_signal_ready = False
        try:
            async with httpx.AsyncClient(timeout=1.0) as http_client:
                res = await http_client.get("http://127.0.0.1:8000/health")
                if res.status_code == 200:
                    is_signal_ready = True
        except (httpx.HTTPError, OSError):
            pass
        states["signal_receiver"] = is_signal_ready
    else:
        states["signal_receiver"] = bool(components.get("signal_receiver", {}).get("ready", False))

    # 6. OEMS Engine (Trading backend engine :8001/health)
    if probe_endpoints:
        is_oems_ready = False
        try:
            async with httpx.AsyncClient(timeout=1.0) as http_client:
                res = await http_client.get("http://127.0.0.1:8001/health")
                if res.status_code == 200:
                    is_oems_ready = True
        except (httpx.HTTPError, OSError):
            pass
        states["oems_engine"] = is_oems_ready
    else:
        states["oems_engine"] = bool(components.get("oems_engine", {}).get("ready", False))

    # 7. Dashboard Engine (HTTP :8010/health)
    if probe_endpoints:
        is_dashboard_ready = False
        try:
            async with httpx.AsyncClient(timeout=1.0) as http_client:
                res = await http_client.get("http://127.0.0.1:8010/health")
                if res.status_code == 200:
                    is_dashboard_ready = True
        except (httpx.HTTPError, OSError):
            pass
        states["dashboard_engine"] = is_dashboard_ready
    else:
        states["dashboard_engine"] = bool(components.get("dashboard_engine", {}).get("ready", False))

    # Format the table
    items_to_render = list(CANONICAL_STARTUP_COMPONENTS)
    canonical_keys = {k for k, _ in CANONICAL_STARTUP_COMPONENTS}
    has_custom = False
    for k, v in components.items():
        if k not in canonical_keys:
            has_custom = True
            states[k] = bool(v.get("ready", False))
            items_to_render.append((k, k.replace("_", " ").title()))

    if has_custom and not any(k in components for k in canonical_keys):
        items_to_render = [(k, k.replace("_", " ").title()) for k in components]

    lines = []
    for key, label in items_to_render:
        ready = states.get(key, False)
        mark = "✓" if ready else "✗"
        lines.append(f"{label:<18} {mark}")

    table = "<pre>\n" + "\n".join(lines) + "\n</pre>"
    return states, table
