#!/usr/bin/env python3
"""
session_guard.py — Evaluates US regular market session window (Mon-Fri 09:30-16:00 ET).
Handles America/New_York timezone and DST transitions dynamically.

Exit codes:
0: Inside market hours (Mon-Fri 09:30-16:00 ET)
1: Outside market hours (Market Closed)
"""
import sys
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

_SESSION_TZ = ZoneInfo("America/New_York")
_SESSION_START = dtime(9, 30)
_SESSION_END = dtime(16, 0)


def is_trading_session(now: datetime | None = None) -> bool:
    """Return True if now (or current time) is within Mon-Fri 09:30-16:00 ET."""
    now_et = (now or datetime.now(_SESSION_TZ)).astimezone(_SESSION_TZ)
    if now_et.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return _SESSION_START <= now_et.time() < _SESSION_END


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
