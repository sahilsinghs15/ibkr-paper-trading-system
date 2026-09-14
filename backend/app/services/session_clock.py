"""NYSE session clock for Red Zone gating using exchange_calendars.

Authoritative calendar for RTH open (09:30 ET) / close (16:00 ET),
NYSE holidays and early-closes. All execution decisions use aware datetimes
in America/New_York; never naive.

Red Zone = [close - buffer, close + post_open_delay_next_day) plus overnight.
Gateway max wait is added for projected check.
"""

from __future__ import annotations

import csv
import logging
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)
HALF_CLOSE = time(13, 0)

# Path to the generated reference/fallback CSV artifact
CSV_PATH = Path(__file__).resolve().parents[2] / "data" / "holidays_xnys.csv"

# Fallback minimal sets for tests
_FALLBACK_HOLIDAYS: set[date] = set()
_FALLBACK_HALF: set[date] = set()

_CSV_CACHE: dict[date, dict[str, str | bool]] | None = None
_CSV_RANGE: tuple[date, date] | None = None


def _load_csv_schedule() -> tuple[dict[date, dict[str, str | bool]], tuple[date, date] | None]:
    """Load reference/fallback NYSE schedule from CSV."""
    global _CSV_CACHE, _CSV_RANGE
    if _CSV_CACHE is not None:
        return _CSV_CACHE, _CSV_RANGE

    schedule: dict[date, dict[str, str | bool]] = {}
    date_range: tuple[date, date] | None = None

    if CSV_PATH.is_file():
        try:
            with open(CSV_PATH, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                dates: list[date] = []
                for row in reader:
                    d_str = row.get("date", "").strip()
                    if not d_str:
                        continue
                    d = date.fromisoformat(d_str)
                    dates.append(d)
                    is_early = str(row.get("is_early_close", "")).strip().lower() == "true"
                    schedule[d] = {
                        "holiday_name": row.get("holiday_name", "").strip(),
                        "is_early_close": is_early,
                        "close_time_et": row.get("close_time_et", "").strip(),
                    }
                if dates:
                    date_range = (min(dates), max(dates))
        except Exception:
            logger.exception("Failed to load holiday reference CSV from %s", CSV_PATH)

    _CSV_CACHE = schedule
    _CSV_RANGE = date_range
    return _CSV_CACHE, _CSV_RANGE


def _get_calendar():
    try:
        import exchange_calendars as xc  # type: ignore

        # Generous calendar range covering 2020 through 2036
        return xc.get_calendar("XNYS", start="2020-01-01", end="2036-01-01")
    except Exception:  # noqa: BLE001
        try:
            import exchange_calendars as xc  # type: ignore

            return xc.get_calendar("XNYS")
        except Exception:  # noqa: BLE001
            return None


_CAL = None


def _cal():
    global _CAL
    if _CAL is None:
        _CAL = _get_calendar()
    return _CAL


def is_trading_day(d: date) -> bool:
    """Canonical trading day check.
    
    1. Primary authority: exchange_calendars (XNYS).
    2. Fallback: generated CSV artifact.
    3. Fail-closed: returns False if neither is available.
       Never fails open to weekday < 5.
    """
    if d in _FALLBACK_HOLIDAYS:
        return False

    cal = _cal()
    if cal is not None:
        try:
            import pandas as pd  # type: ignore

            ts = pd.Timestamp(d)
            return bool(cal.is_session(ts))
        except Exception:  # noqa: BLE001
            logger.debug("Failed checking session in exchange_calendars for %s", d)

    # Fallback to CSV artifact
    csv_schedule, csv_range = _load_csv_schedule()
    if csv_schedule is not None and csv_range is not None:
        min_d, max_d = csv_range
        if min_d <= d <= max_d:
            if d.weekday() >= 5:
                return False
            entry = csv_schedule.get(d)
            if entry is not None:
                return bool(entry["is_early_close"])
            return True

    # Calendar and CSV both unavailable or out-of-range -> FAIL CLOSED
    return False


def get_holiday_reason(d: date) -> str:
    """Return descriptive reason why market is closed (holiday name or weekend day)."""
    weekday = d.weekday()
    if weekday == 5:
        return "Saturday"
    if weekday == 6:
        return "Sunday"

    cal = _cal()
    if cal is not None:
        try:
            import pandas as pd  # type: ignore

            ts = pd.Timestamp(d)
            if not cal.is_session(ts):
                h = cal.regular_holidays.holidays(ts, ts, return_name=True)  # pyrefly: ignore[missing-attribute]
                if not h.empty:
                    name = str(h.iloc[0]).strip()
                    if name == "July 4th":
                        return "Independence Day"
                    if name == "President's Day":
                        return "Presidents' Day"
                    return name
        except Exception:  # noqa: BLE001
            logger.debug("Failed resolving regular holiday from calendar for %s", d)
        try:
            for adh in getattr(cal, "adhoc_holidays", []):
                import pandas as pd  # type: ignore

                adh_d = adh.date() if isinstance(adh, (pd.Timestamp, datetime)) else adh
                if adh_d == d:
                    return "Special Non-Trading Day"
        except Exception:  # noqa: BLE001
            logger.debug("Failed resolving adhoc holiday from calendar for %s", d)

    csv_schedule, _ = _load_csv_schedule()
    if csv_schedule and d in csv_schedule:
        entry = csv_schedule[d]
        if not entry["is_early_close"]:
            return str(entry["holiday_name"])

    if not is_trading_day(d):
        return "NYSE Holiday"
    return ""


def rth_open_for(d: date) -> datetime | None:
    if not is_trading_day(d):
        return None
    cal = _cal()
    if cal is not None:
        try:
            import pandas as pd  # type: ignore

            if not cal.is_session(pd.Timestamp(d)):
                return None
            sched = cal.schedule.loc[pd.Timestamp(d)]
            o = sched["open"]
            return o.tz_convert(ET)  # type: ignore
        except (KeyError, ValueError, AttributeError, RuntimeError):
            pass
    return datetime.combine(d, RTH_OPEN, tzinfo=ET)


def rth_close_for(d: date) -> datetime | None:
    if not is_trading_day(d):
        return None
    if d in _FALLBACK_HALF:
        return datetime.combine(d, HALF_CLOSE, tzinfo=ET)
    cal = _cal()
    if cal is not None:
        try:
            import pandas as pd  # type: ignore

            if not cal.is_session(pd.Timestamp(d)):
                return None
            sched = cal.schedule.loc[pd.Timestamp(d)]
            c = sched["close"]
            return c.tz_convert(ET)  # type: ignore
        except (KeyError, ValueError, AttributeError, RuntimeError):
            pass
    csv_schedule, _ = _load_csv_schedule()
    if csv_schedule and d in csv_schedule:
        entry = csv_schedule[d]
        if entry["is_early_close"] and entry["close_time_et"]:
            parts = str(entry["close_time_et"]).split(":")
            return datetime.combine(d, time(int(parts[0]), int(parts[1])), tzinfo=ET)
    return datetime.combine(d, RTH_CLOSE, tzinfo=ET)


def next_trading_day(d: date) -> date:
    cal = _cal()
    if cal is not None:
        try:
            import pandas as pd  # type: ignore

            nxt = cal.next_open(pd.Timestamp(d)).date()  # type: ignore
            if isinstance(nxt, date) and not isinstance(nxt, datetime):
                return nxt
            return nxt  # type: ignore
        except (KeyError, ValueError, AttributeError, RuntimeError):
            pass
    cur = d + timedelta(days=1)
    for _ in range(15):
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
        except (KeyError, ValueError, AttributeError, RuntimeError):
            pass
    cur = d - timedelta(days=1)
    for _ in range(15):
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
