#!/usr/bin/env python3
"""Standalone Telegram helper — observational only.

Reuse WatchdogSettings (TELEGRAM_BOT_TOKEN/CHAT_ID/ENABLED).
Never fails parent systemd service. Short timeout, no systemctl calls.
Supports: start <service> | stop <service> | market-closed
"""
from __future__ import annotations
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# ensure backend on path
_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

ET = ZoneInfo("America/New_York")
STATE_FILE = Path("/home/tradingapp/storage/state/notify-telegram-market-closed.json")
# also respect /tmp for tests via env override
if "NOTIFY_STATE_FILE" in __import__("os").environ:
    STATE_FILE = Path(__import__("os").environ["NOTIFY_STATE_FILE"])

ALLOWED_SERVICES = {"ibgateway", "trading-backend", "demo-streaming", "webhook-ingest"}

logger = logging.getLogger("notify-telegram")


def _load_settings():
    try:
        from app.services.watchdog.config import get_watchdog_settings
        return get_watchdog_settings()
    except Exception:
        return None


def _send(text: str) -> bool:
    settings = _load_settings()
    if settings is None or not getattr(settings, "telegram_enabled", False):
        return False
    token = getattr(settings, "telegram_bot_token", None)
    chat = getattr(settings, "telegram_chat_id", None)
    if not token or not chat:
        return False
    try:
        from app.services.watchdog.telegram import TelegramClient
        client = TelegramClient(
            bot_token=token,
            chat_id=chat,
            timeout=getattr(settings, "telegram_timeout_seconds", 5.0),
            max_retries=1,  # helper should be quick, not 3 retries
            rate_limit_per_sec=getattr(settings, "telegram_rate_limit_per_sec", 1.0),
            enabled=True,
        )
        return asyncio.run(client.send_message(text))
    except Exception as exc:  # never propagate
        logger.debug("notify-telegram send failed: %s", exc)
        return False


def _market_closed_reason(today) -> str:
    # today is date in ET
    weekday = today.weekday()  # 0 Mon
    if weekday == 5:
        return "Saturday"
    if weekday == 6:
        return "Sunday"
    # holiday: try to get name from exchange_calendars, fallback generic
    try:
        from app.services.session_clock import _cal
        import pandas as pd
        cal = _cal()
        if cal is not None:
            # exchange_calendars doesn't expose holiday name directly; use best effort
            # Check if today is holiday by is_session false and weekday <5
            # Try to find holiday name via cal.schedule vs known list
            # Fallback to generic with date
            pass
    except Exception:
        pass
    # Generic holiday name fallback — keep concise
    # Could map known US holidays by month/day for nicer names, else "Holiday"
    # Simple mapping for common NYSE holidays
    md = (today.month, today.day)
    holiday_names = {
        (1, 1): "New Year's Day",
        (7, 4): "Independence Day",
        (12, 25): "Christmas Day",
    }
    if md in holiday_names:
        return holiday_names[md]
    # Check known 2024+ Labor Day etc via calendar: use generic
    return "Holiday"


def do_market_closed() -> int:
    from app.services.session_clock import is_trading_day
    today_et = datetime.now(ET).date()
    # Early-close is trading day → no message
    if is_trading_day(today_et):
        return 0
    reason = _market_closed_reason(today_et)
    # dedup: once per ET date
    state_date = None
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text())
            state_date = data.get("date")
    except Exception:
        state_date = None
    today_s = today_et.isoformat()
    if state_date == today_s:
        return 0
    text = f"\U0001f4c5 Market closed \u2014 {reason}"
    ok = _send(text)
    # write state even if telegram disabled/failed to avoid spamming retries? No — only write on success or if disabled we still mark dedup to avoid daily spam?
    # For observational safety, mark dedup on attempt (success or disabled) to prevent repeated timer firing.
    # But if send failed (network), we still persist to avoid tight loop? We persist anyway with date.
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({"date": today_s, "reason": reason}))
    except Exception:
        pass
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: notify-telegram.py start|stop <service> | market-closed", file=sys.stderr)
        return 0
    cmd = argv[1].lower()
    try:
        if cmd == "market-closed":
            return do_market_closed()
        if cmd in ("start", "stop") and len(argv) >= 3:
            svc = argv[2].strip()
            if svc not in ALLOWED_SERVICES:
                # allow suffix .service
                svc = svc.replace(".service", "")
                if svc not in ALLOWED_SERVICES:
                    return 0
            verb = "started" if cmd == "start" else "stopped"
            icon = "\U0001f7e2" if cmd == "start" else "\U0001f534"
            text = f"{icon} {svc} {verb}"
            _send(text)
            return 0
        return 0
    except Exception:
        # never fail systemd
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
