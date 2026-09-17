"""Canonical human-readable notification dictionary and formatting contract.

Shared across Telegram notifications and Frontend Notification Center.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

CANONICAL_SERVICES: dict[str, dict[str, Any]] = {
    "ibgateway": {
        "friendly_name": "Broker Engine",
        "unit": "ibgateway.service",
        "SERVICE_STARTED": {"icon": "🟢", "message": "Broker Engine Started Successfully"},
        "SERVICE_STOPPED": {"icon": "🔴", "message": "Broker Engine Stopped"},
    },
    "trading-backend": {
        "friendly_name": "OEMS Engine",
        "unit": "trading-backend.service",
        "SERVICE_STARTED": {"icon": "🟢", "message": "OEMS Engine Started Successfully"},
        "SERVICE_STOPPED": {"icon": "🔴", "message": "OEMS Engine Stopped"},
    },
    "webhook-ingest": {
        "friendly_name": "Signal Receiver",
        "unit": "webhook-ingest.service",
        "SERVICE_STARTED": {"icon": "🟢", "message": "Signal Receiver Started Successfully"},
        "SERVICE_STOPPED": {"icon": "🔴", "message": "Signal Receiver Stopped"},
    },
    "demo-streaming": {
        "friendly_name": "Dashboard Engine",
        "unit": "demo-streaming.service",
        "SERVICE_STARTED": {"icon": "🟢", "message": "Dashboard Engine Started Successfully"},
        "SERVICE_STOPPED": {"icon": "🔴", "message": "Dashboard Engine Stopped"},
    },
}

ALLOWED_SERVICES = frozenset(CANONICAL_SERVICES.keys())
ALLOWED_KINDS = frozenset(
    {
        "SERVICE_STARTED",
        "SERVICE_STOPPED",
        "MARKET_CLOSED",
        "ROGUE_TRADE_DETECTED",
        "ROGUE_TRADE_RESOLVED",
        "LOSS_THRESHOLD_BREACHED",
    }
)


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

    if kind == "ROGUE_TRADE_DETECTED":
        rogue_type = detail.get("rogue_type") or "QTY_DRIFT"
        symbol = detail.get("symbol") or "Unknown"
        acc = detail.get("ibkr_account") or detail.get("account_id")
        acc_str = f" ({acc})" if acc else ""
        type_label = {
            "QTY_DRIFT": "Quantity Difference",
            "BROKER_ORPHAN": "Broker Ghost",
            "LEDGER_GHOST": "Ledger Ghost",
        }.get(str(rogue_type), str(rogue_type))
        title = f"Rogue trade: {type_label} on {symbol}{acc_str}"
        # Prefer canonical message from reconciler (includes broker/ledger qty)
        _rogue_msg: Any = detail.get("message")
        if not _rogue_msg:
            broker_qty = detail.get("broker_qty")
            ledger_qty = detail.get("ledger_qty")
            _rogue_msg = f"{title}. Broker qty: {broker_qty}, Ledger qty: {ledger_qty}"
        rogue_msg: str = str(_rogue_msg)
        return {
            "icon": "🚨",
            "title": title,
            "message": rogue_msg,
            "friendly_name": "Rogue trade detection",
            "service": "reconcile",
            "unit": "trading-backend.service",
        }

    if kind == "ROGUE_TRADE_RESOLVED":
        symbol = detail.get("symbol") or "Unknown"
        acc = detail.get("ibkr_account") or detail.get("account_id")
        acc_str = f" ({acc})" if acc else ""
        rogue_type = detail.get("rogue_type") or ""
        title = f"Rogue trade resolved: {symbol}{acc_str}"
        _msg2: Any = detail.get("message")
        if _msg2:
            msg2: str = str(_msg2)
        else:
            msg2 = (
                f"🟢 ROGUE TRADE RESOLVED: {rogue_type} on {symbol} ({acc}). "
                f"Discrepancy cleared."
                if rogue_type
                else f"Discrepancy for {symbol}{acc_str} has been resolved."
            )
        return {
            "icon": "🟢",
            "title": title,
            "message": msg2,
            "friendly_name": "Rogue trade detection",
            "service": "reconcile",
            "unit": "trading-backend.service",
        }

    if kind == "LOSS_THRESHOLD_BREACHED":
        acc = detail.get("ibkr_account") or detail.get("account_id") or "Unknown"
        realised = detail.get("realized_pnl") or detail.get("realized_pnl_str") or "0"
        thresh = detail.get("loss_threshold") or detail.get("loss_threshold_str") or "0"
        ts = detail.get("timestamp") or ""
        try:
            rv = float(realised)
            tv = float(thresh)
            realised_str = f"${rv:,.2f}"
            thresh_str = f"${tv:,.2f}"
        except Exception:  # noqa: BLE001
            realised_str = str(realised)
            thresh_str = str(thresh)
        title = "Loss threshold breached"
        msg = f"🔴 Loss threshold breached\nAccount: {acc}\nRealized P&L: {realised_str}\nLoss Threshold: {thresh_str}"
        if ts:
            msg = f"{msg}\nTimestamp: {ts}"
        return {
            "icon": "🔴",
            "title": title,
            "message": msg,
            "friendly_name": "Risk monitoring",
            "service": "risk",
            "unit": "trading-backend.service",
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


async def send_canonical_telegram(
    kind: str,
    detail: dict[str, Any] | None,
) -> bool:
    """DEPRECATED — legacy direct Telegram sender.

    Retained only for backward compatibility with tests that patch this symbol.
    All production notifications must go through NotificationOrchestrator →
    NotificationDeliveryWorker → TelegramChannelAdapter.

    The legacy path is intentionally disabled; this function never sends Telegram
    messages regardless of settings. It logs a debug message and returns True so
    callers that still import it do not fail, but no external HTTP is performed.
    """
    logger.debug(
        "send_canonical_telegram is deprecated and disabled (single centralized system). kind=%s",
        kind,
    )
    return True
