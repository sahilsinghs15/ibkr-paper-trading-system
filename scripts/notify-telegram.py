#!/usr/bin/env python3
"""Standalone Telegram and event_log persistence helper.

Never fails parent systemd service. Short timeout, no systemctl calls.
Supports: start <service> | stop <service> | market-closed
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Ensure backend and repository root on path
_REPO_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _REPO_ROOT / "backend"
for _p in (str(_BACKEND), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_ENV_PATH = _BACKEND / ".env"
if _ENV_PATH.is_file():
    try:
        from dotenv import load_dotenv

        load_dotenv(_ENV_PATH)
    except ImportError:
        pass

os.environ.setdefault("WEBHOOK_AUTH_SECRET", "standalone_script_dummy")

ET = ZoneInfo("America/New_York")

try:
    from app.services import session_clock
except ImportError:
    from backend.app.services import session_clock  # type: ignore

try:
    from app.db.repositories.event_repository import EventRepository
    from app.db.session import AsyncSessionLocal, engine
except ImportError:
    from backend.app.db.repositories.event_repository import (
        EventRepository,  # type: ignore
    )
    from backend.app.db.session import AsyncSessionLocal, engine  # type: ignore


def _get_state_file() -> Path:
    if "NOTIFY_STATE_FILE" in os.environ:
        return Path(os.environ["NOTIFY_STATE_FILE"])
    p = Path("/home/tradingapp/storage/state")
    if p.exists() or Path("/home/tradingapp").exists():
        return p / "notify-telegram-market-closed.json"
    repo_state = _REPO_ROOT / "storage" / "state"
    return repo_state / "notify-telegram-market-closed.json"


ALLOWED_SERVICES = {"ibgateway", "trading-backend", "demo-streaming", "webhook-ingest"}

logger = logging.getLogger("notify-telegram")


def _send(text: str) -> bool:
    try:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat = os.environ.get("TELEGRAM_CHAT_ID")
        enabled = os.environ.get("TELEGRAM_ENABLED", "").lower() in ("true", "1", "yes")

        if not (token and chat and enabled):
            try:
                from app.services.watchdog.config import get_watchdog_settings

                settings = get_watchdog_settings()
                if (
                    settings is not None
                    and getattr(settings, "telegram_enabled", False)
                ):
                    token = getattr(settings, "telegram_bot_token", token)
                    chat = getattr(settings, "telegram_chat_id", chat)
                    enabled = True
            except Exception as exc:  # noqa: BLE001
                logger.debug("Failed loading watchdog settings: %s", exc)

        if not (enabled and token and chat):
            return False

        try:
            from app.services.watchdog.telegram import TelegramClient

            client = TelegramClient(
                bot_token=token,
                chat_id=chat,
                timeout=5.0,
                max_retries=1,
                rate_limit_per_sec=1.0,
                enabled=True,
            )
            return asyncio.run(client.send_message(text))
        except Exception:  # noqa: BLE001
            import httpx

            async def _direct_send() -> bool:
                url = f"https://api.telegram.org/bot{token}/sendMessage"
                payload = {"chat_id": chat, "text": text, "parse_mode": "HTML"}
                async with httpx.AsyncClient(timeout=5.0) as http_client:
                    resp = await http_client.post(url, json=payload)
                    return resp.status_code == 200

            return asyncio.run(_direct_send())
    except Exception as exc:  # noqa: BLE001
        logger.debug("notify-telegram send failed: %s", exc)
        return False


def _persist_event(
    process: str,
    kind: str,
    detail: dict,
    idempotency_key: str | None = None,
) -> bool:
    """Durably record lifecycle/market event to PostgreSQL event_log."""
    try:
        async def _run() -> None:
            try:
                async with AsyncSessionLocal() as session:
                    repo = EventRepository(session)
                    await repo.append(
                        process=process,
                        kind=kind,
                        detail=detail,
                        idempotency_key=idempotency_key,
                    )
                    await session.commit()
            finally:
                await engine.dispose()

        asyncio.run(_run())
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("notify-telegram event persist failed: %s", exc)
        return False


def do_market_closed() -> int:
    today_et = datetime.now(ET).date()
    # Early-close is trading day -> no message
    if session_clock.is_trading_day(today_et):
        return 0

    reason = session_clock.get_holiday_reason(today_et) or "Market Closed"
    today_s = today_et.isoformat()
    text = f"📅 Market closed — {reason}"
    idempotency_key = f"market_closed:{today_s}"
    detail = {
        "date": today_s,
        "reason": reason,
        "icon": "📅",
        "message": f"Market closed — {reason}",
    }

    # 1. Persist to PostgreSQL event_log (idempotent)
    _persist_event(
        process="session_clock",
        kind="MARKET_CLOSED",
        detail=detail,
        idempotency_key=idempotency_key,
    )

    # 2. Dedup Telegram send: once per ET date
    state_file = _get_state_file()
    state_date = None
    try:
        if state_file.exists():
            data = json.loads(state_file.read_text(encoding="utf-8"))
            state_date = data.get("date")
    except (OSError, json.JSONDecodeError) as err:
        logger.debug("Could not read state file: %s", err)
        state_date = None

    if state_date == today_s:
        return 0

    _send(text)

    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(
            json.dumps({"date": today_s, "reason": reason}),
            encoding="utf-8",
        )
    except OSError as err:
        logger.debug("Could not write state file: %s", err)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            "usage: notify-telegram.py start|stop <service> | market-closed",
            file=sys.stderr,
        )
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
            icon = "🟢" if cmd == "start" else "🔴"
            kind = "SERVICE_STARTED" if cmd == "start" else "SERVICE_STOPPED"
            text = f"{icon} {svc} {verb}"

            now_ts = int(time.time())
            inv_id = os.environ.get("INVOCATION_ID") or f"{now_ts}_{os.getpid()}"
            idempotency_key = f"service:{svc}:{cmd}:{inv_id}"
            detail = {
                "service": svc,
                "unit": f"{svc}.service",
                "action": cmd,
                "icon": icon,
                "message": f"{svc} {verb}",
            }

            # 1. Persist to PostgreSQL event_log
            _persist_event(
                process="systemd",
                kind=kind,
                detail=detail,
                idempotency_key=idempotency_key,
            )

            # 2. Send Telegram
            _send(text)
            return 0
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.debug("notify-telegram unhandled exception: %s", exc)
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
