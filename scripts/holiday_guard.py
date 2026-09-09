#!/usr/bin/env python3
"""holiday_guard.py — Startup safety guard for NYSE trading sessions.

Exit codes:
0: Trading day / startup allowed.
1: Non-trading day (NYSE holiday or weekend) / startup blocked.
2: Calendar error / fail-closed safety block.

Ensures MARKET_CLOSED event is persisted into PostgreSQL event_log and
Telegram notification is sent BEFORE any trading process is launched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import date, datetime
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


logger = logging.getLogger("holiday_guard")


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
        logger.warning("Failed to persist event to event_log: %s", exc)
        return False


def _send_telegram(text: str) -> bool:
    """Send Telegram notification using existing configuration."""
    try:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat = os.environ.get("TELEGRAM_CHAT_ID")
        enabled = os.environ.get("TELEGRAM_ENABLED", "").lower() in ("true", "1", "yes")

        # Try loading watchdog settings if env vars not fully set
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
        logger.debug("Telegram notification failed: %s", exc)
        return False


def check_trading_day(target_date: date | None = None) -> int:
    """Check trading day status.

    Returns:
        0: Allowed (active NYSE trading session).
        1: Non-trading day (holiday or weekend).
        2: Calendar error (fail-closed safety block).
    """
    try:
        target = target_date or datetime.now(ET).date()
        allowed = session_clock.is_trading_day(target)

        if allowed:
            print(
                f"Trading session allowed: {target.isoformat()} is an active "
                "NYSE trading day."
            )
            return 0

        reason = session_clock.get_holiday_reason(target) or "Market Closed"
        today_s = target.isoformat()
        message = f"Market closed — {reason}"
        text = f"📅 {message}"
        idempotency_key = f"market_closed:{today_s}"
        detail = {
            "date": today_s,
            "reason": reason,
            "icon": "📅",
            "message": message,
        }

        # 1. Persist to PostgreSQL event_log
        _persist_event(
            process="session_clock",
            kind="MARKET_CLOSED",
            detail=detail,
            idempotency_key=idempotency_key,
        )

        # 2. Deduplicated Telegram alert
        state_file = _get_state_file()
        state_date = None
        try:
            if state_file.exists():
                data = json.loads(state_file.read_text(encoding="utf-8"))
                state_date = data.get("date")
        except (OSError, json.JSONDecodeError) as err:
            logger.debug("Could not read state file: %s", err)
            state_date = None

        if state_date != today_s:
            _send_telegram(text)
            try:
                state_file.parent.mkdir(parents=True, exist_ok=True)
                state_file.write_text(
                    json.dumps({"date": today_s, "reason": reason}),
                    encoding="utf-8",
                )
            except OSError as err:
                logger.debug("Could not write state file: %s", err)

        print(
            f"Trading session blocked: {today_s} is non-trading ({reason}).",
            file=sys.stderr,
        )
        return 1

    except Exception as exc:  # noqa: BLE001
        print(f"Safety guard error (failing closed): {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NYSE Trading Day Startup Guard")
    parser.add_argument(
        "--date",
        type=str,
        help="Optional ISO date (YYYY-MM-DD) to check instead of current ET date",
    )
    args = parser.parse_args(argv)

    target_d = date.fromisoformat(args.date) if args.date else None
    return check_trading_day(target_d)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
