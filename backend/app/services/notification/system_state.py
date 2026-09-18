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
    if "ec2_instance" in components:
        states["ec2_instance"] = bool(components["ec2_instance"].get("ready", False))
    else:
        # Host environment is active by virtue of this code executing
        states["ec2_instance"] = True

    # 2. IB Gateway (systemd ibgateway.service / process check)
    if "ib_gateway" in components:
        states["ib_gateway"] = bool(components["ib_gateway"].get("ready", False))
    elif probe_endpoints:
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
        states["ib_gateway"] = False

    # Probe backend readiness for broker status when client is not passed (e.g. external CLI)
    is_tws_ready_probe = False
    if client is None and probe_endpoints and not components.get("oems_engine", {}).get("ready") is False:
        try:
            async with httpx.AsyncClient(timeout=1.0) as http_client:
                res = await http_client.get("http://127.0.0.1:8001/health/ready")
                if res.status_code == 200 and res.json().get("status") == "ok":
                    is_tws_ready_probe = True
        except (httpx.HTTPError, OSError):
            pass

    # Check if IB Gateway API port (4002) is open (proves IB Gateway login is complete)
    is_gateway_api_open = False
    if probe_endpoints and states.get("ib_gateway", False) and not components.get("ib_gateway", {}).get("ready") is False:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", 4002),
                timeout=1.0,
            )
            writer.close()
            await writer.wait_closed()
            is_gateway_api_open = True
        except (TimeoutError, OSError):
            pass

    # 3. IB Login (authoritative nextValidId / authenticated handshake or port 4002 open)
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
        states["ib_login"] = bool(is_tws_ready_probe or is_gateway_api_open)

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
        states["broker_connection"] = is_tws_ready_probe

    # 5. Signal Receiver (HTTP :8000/health)
    if "signal_receiver" in components:
        states["signal_receiver"] = bool(components["signal_receiver"].get("ready", False))
    elif probe_endpoints:
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
        states["signal_receiver"] = False

    # 6. OEMS Engine (Trading backend engine :8001/health)
    if "oems_engine" in components:
        states["oems_engine"] = bool(components["oems_engine"].get("ready", False))
    elif client is not None:
        # Client instance is provided when executing from within the trading backend (OEMS engine) process
        states["oems_engine"] = True
    elif probe_endpoints:
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
        states["oems_engine"] = False

    # 7. Dashboard Engine (HTTP :8010/health)
    if "dashboard_engine" in components:
        states["dashboard_engine"] = bool(components["dashboard_engine"].get("ready", False))
    elif probe_endpoints:
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
        states["dashboard_engine"] = False

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
