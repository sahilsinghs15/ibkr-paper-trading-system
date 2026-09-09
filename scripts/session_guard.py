#!/usr/bin/env python3
"""session_guard.py — Evaluates US regular market session window.

Uses canonical SessionClock / XNYS calendar authority with CSV fallback.
Handles America/New_York timezone, DST transitions, NYSE holidays, and early closes.

Exit codes:
0: Inside market hours
1: Outside market hours (Market Closed / Holiday / Weekend)
"""

import sys
from datetime import datetime
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

# Ensure backend and repository root on path
_REPO_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _REPO_ROOT / "backend"
for _p in (str(_BACKEND), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from app.services import session_clock
except ImportError:
    from backend.app.services import session_clock  # type: ignore

_SESSION_TZ = ZoneInfo("America/New_York")
_SESSION_START = dtime(9, 30)
_DEFAULT_SESSION_END = dtime(16, 0)


def is_trading_session(now: datetime | None = None) -> bool:
    """Return True if now (or current time) is within active NYSE trading hours."""
    if now is None:
        now_et = datetime.now(_SESSION_TZ)
    elif now.tzinfo is None:
        now_et = now.replace(tzinfo=_SESSION_TZ)
    else:
        now_et = now.astimezone(_SESSION_TZ)

    d = now_et.date()

    try:
        if not session_clock.is_trading_day(d):
            return False

        close_dt = session_clock.rth_close_for(d)
        session_end = close_dt.time() if close_dt is not None else _DEFAULT_SESSION_END
        return _SESSION_START <= now_et.time() < session_end
    except Exception:  # noqa: BLE001
        # Fail closed on calendar error
        return False


def main() -> int:
    now_et = datetime.now(_SESSION_TZ)
    in_session = is_trading_session(now_et)

    if len(sys.argv) > 1 and sys.argv[1] in ("info", "--info"):
        print(f"Time (ET): {now_et.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        print(f"Day: {now_et.strftime('%A')}")
        print(f"Trading Session Active: {in_session}")
        return 0 if in_session else 1

    return 0 if in_session else 1


if __name__ == "__main__":
    sys.exit(main())
