"""Canonical human-readable notification dictionary and formatting contract.

Shared across Telegram notifications and Frontend Notification Center.
"""

from __future__ import annotations

from typing import Any

CANONICAL_SERVICES: dict[str, dict[str, Any]] = {
    "ibgateway": {
        "friendly_name": "Broker connection",
        "unit": "ibgateway.service",
        "SERVICE_STARTED": {"icon": "🟢", "message": "Broker connection started"},
        "SERVICE_STOPPED": {"icon": "🔴", "message": "Broker connection stopped"},
    },
    "trading-backend": {
        "friendly_name": "Trading system",
        "unit": "trading-backend.service",
        "SERVICE_STARTED": {"icon": "🟢", "message": "Trading system started"},
        "SERVICE_STOPPED": {"icon": "🔴", "message": "Trading system stopped"},
    },
    "webhook-ingest": {
        "friendly_name": "Market signal intake",
        "unit": "webhook-ingest.service",
        "SERVICE_STARTED": {"icon": "🟢", "message": "Market signal intake started"},
        "SERVICE_STOPPED": {"icon": "🔴", "message": "Market signal intake stopped"},
    },
    "demo-streaming": {
        "friendly_name": "Market data display",
        "unit": "demo-streaming.service",
        "SERVICE_STARTED": {"icon": "🟢", "message": "Market data display started"},
        "SERVICE_STOPPED": {"icon": "🔴", "message": "Market data display stopped"},
    },
}

ALLOWED_SERVICES = frozenset(CANONICAL_SERVICES.keys())
ALLOWED_KINDS = frozenset({"SERVICE_STARTED", "SERVICE_STOPPED", "MARKET_CLOSED"})


def format_canonical_notification(
    kind: str,
    detail: dict[str, Any] | None,
) -> dict[str, Any]:
    """Format canonical operator-friendly notification while preserving technical details."""
    detail = detail or {}
    service = detail.get("service")

    if kind in ("SERVICE_STARTED", "SERVICE_STOPPED"):
        svc_cfg = CANONICAL_SERVICES.get(str(service))
        if svc_cfg and kind in svc_cfg:
            cfg = svc_cfg[kind]
            return {
                "icon": cfg["icon"],
                "title": cfg["message"],
                "message": cfg["message"],
                "friendly_name": svc_cfg["friendly_name"],
                "service": service,
                "unit": svc_cfg["unit"],
            }
        # Fallback for unrecognized service
        action = "started" if kind == "SERVICE_STARTED" else "stopped"
        icon = "🟢" if kind == "SERVICE_STARTED" else "🔴"
        return {
            "icon": icon,
            "title": f"{service or 'Service'} {action}",
            "message": f"{service or 'Service'} {action}",
            "friendly_name": service or "Service",
            "service": service,
            "unit": detail.get("unit") or f"{service}.service",
        }

    if kind == "MARKET_CLOSED":
        reason = detail.get("reason") or "Market Closed"
        msg = f"Market closed — {reason}"
        return {
            "icon": "📅",
            "title": msg,
            "message": msg,
            "friendly_name": "Market status",
            "service": None,
            "unit": None,
        }

    # Generic fallback
    return {
        "icon": str(detail.get("icon") or "ℹ️"),
        "title": str(detail.get("message") or kind),
        "message": str(detail.get("message") or kind),
        "friendly_name": "System",
        "service": service,
        "unit": detail.get("unit"),
    }
