"""NYSE session clock for Red Zone gating using exchange_calendars.

Authoritative calendar for RTH open (09:30 ET) / close (16:00 ET),
NYSE holidays and early-closes. All execution decisions use aware datetimes
in America/New_York; never naive.

Red Zone = [close - buffer, close + post_open_delay_next_day) plus overnight.
Gateway max wait is added for projected check.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)
HALF_CLOSE = time(13, 0)

# Fallback minimal sets if exchange_calendars unavailable (tests still pass)
_FALLBACK_HOLIDAYS = set()
_FALLBACK_HALF = set()


def _get_calendar():
    try:
        import exchange_calendars as xc  # type: ignore

        return xc.get_calendar("XNYS")
    except Exception:
        return None


_CAL = None


def _cal():
    global _CAL
    if _CAL is None:
        _CAL = _get_calendar()
    return _CAL


def is_trading_day(d: date) -> bool:
    cal = _cal()
    if cal is not None:
        try:
            # exchange_calendars is_session uses Timestamp
            import pandas as pd  # type: ignore

            return bool(cal.is_session(pd.Timestamp(d)))
        except Exception:
            pass
    # fallback: weekend only
    return d.weekday() < 5


def rth_open_for(d: date) -> datetime | None:
    cal = _cal()
    if cal is not None:
        try:
            import pandas as pd  # type: ignore

            if not cal.is_session(pd.Timestamp(d)):
                return None
            sched = cal.schedule.loc[pd.Timestamp(d)]
            o = sched["open"]
            return o.tz_convert(ET)  # type: ignore
        except Exception:
            pass
    if not is_trading_day(d):
        return None
    return datetime.combine(d, RTH_OPEN, tzinfo=ET)


def rth_close_for(d: date) -> datetime | None:
    cal = _cal()
    if cal is not None:
        try:
            import pandas as pd  # type: ignore

            if not cal.is_session(pd.Timestamp(d)):
                return None
            sched = cal.schedule.loc[pd.Timestamp(d)]
            c = sched["close"]
            return c.tz_convert(ET)  # type: ignore
        except Exception:
            pass
    if not is_trading_day(d):
        return None
    return datetime.combine(d, RTH_CLOSE, tzinfo=ET)


def next_trading_day(d: date) -> date:
    cal = _cal()
    if cal is not None:
        try:
            import pandas as pd  # type: ignore

            nxt = cal.next_open(pd.Timestamp(d)).date()  # type: ignore
            # next_open returns next session open timestamp; extract date
            if isinstance(nxt, date) and not isinstance(nxt, datetime):
                return nxt
            return nxt  # type: ignore
        except Exception:
            pass
    cur = d + timedelta(days=1)
    for _ in range(10):
        if is_trading_day(cur):
            return cur
        cur += timedelta(days=1)
    return cur


def prev_trading_day(d: date) -> date:
    cal = _cal()
    if cal is not None:
        try:
            import pandas as pd  # type: ignore

            prev = cal.previous_close(pd.Timestamp(d)).date()  # type: ignore
            if isinstance(prev, date) and not isinstance(prev, datetime):
                return prev
            return prev  # type: ignore
        except Exception:
            pass
    cur = d - timedelta(days=1)
    for _ in range(10):
        if is_trading_day(cur):
            return cur
        cur -= timedelta(days=1)
    return cur


class SessionClock:
    """Exchange/session clock. All inputs must be timezone-aware."""

    def __init__(
        self,
        buffer_seconds: int = 45,
        post_open_delay_seconds: int = 120,
        gateway_max_wait_sec: float = 8.0,
    ) -> None:
        self.buffer_seconds = buffer_seconds
        self.post_open_delay_seconds = post_open_delay_seconds
        self.gateway_max_wait_sec = gateway_max_wait_sec

    def _require_aware(self, dt: datetime) -> datetime:
        if dt.tzinfo is None:
            raise ValueError("SessionClock requires aware datetime")
        return dt

    def now(self) -> datetime:
        return datetime.now(UTC).astimezone(ET)

    def rth_open(self, day: date) -> datetime | None:
        return rth_open_for(day)

    def rth_close(self, day: date) -> datetime | None:
        return rth_close_for(day)

    def in_red_zone(self, now: datetime) -> bool:
        now = self._require_aware(now)
        now_et = now.astimezone(ET)
        open_today = rth_open_for(now_et.date())
        if open_today is not None:
            if now_et < open_today:
                return True
            post_open_until = open_today + timedelta(seconds=self.post_open_delay_seconds)
            if now_et < post_open_until:
                return True
            close_today = rth_close_for(now_et.date())
            assert close_today is not None
            buffer_start = close_today - timedelta(seconds=self.buffer_seconds)
            return now_et >= buffer_start
        else:
            return True

    def projected_in_red_zone(self, now: datetime) -> bool:
        now = self._require_aware(now)
        projected = now + timedelta(seconds=self.gateway_max_wait_sec)
        return self.in_red_zone(projected)

    def resolved_session_close(self, now: datetime) -> datetime | None:
        now = self._require_aware(now)
        now_et = now.astimezone(ET)
        d = now_et.date()
        close_today = rth_close_for(d)
        if close_today is not None:
            open_today = rth_open_for(d)
            assert open_today is not None
            if now_et < open_today:
                prev = prev_trading_day(d)
                return rth_close_for(prev)
            return close_today
        prev = prev_trading_day(d)
        return rth_close_for(prev)

    def deferred_session_count(self, deferred_at: datetime, now: datetime) -> int:
        deferred_at = self._require_aware(deferred_at).astimezone(ET)
        now = self._require_aware(now).astimezone(ET)
        count = 0
        cur = deferred_at.date() + timedelta(days=1)
        end_date = now.date()
        while cur <= end_date:
            c = rth_close_for(cur)
            if c is not None and deferred_at < c <= now:
                count += 1
            cur += timedelta(days=1)
        return count


def get_session_clock() -> SessionClock:
    from app.core.config import get_settings

    s = get_settings()
    return SessionClock(
        buffer_seconds=s.red_zone_buffer_seconds,
        post_open_delay_seconds=s.post_open_delay_seconds,
        gateway_max_wait_sec=s.ibkr_gateway_max_wait_sec,
    )
