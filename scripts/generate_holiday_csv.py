#!/usr/bin/env python3
"""Generate NYSE (XNYS) holidays and early-close reference CSV.

Artifact path: backend/data/holidays_xnys.csv
Schema: date,holiday_name,is_early_close,close_time_et
Horizon: 2024-01-01 through 2035-12-31.
"""

from __future__ import annotations

import csv
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars as xc  # type: ignore
import pandas as pd  # type: ignore

ET = ZoneInfo("America/New_York")
_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV_PATH = _REPO_ROOT / "backend" / "data" / "holidays_xnys.csv"


def _clean_holiday_name(name: str) -> str:
    cleaned = name.strip()
    if cleaned == "July 4th":
        return "Independence Day"
    if cleaned == "President's Day":
        return "Presidents' Day"
    return cleaned


def _get_early_close_name(d: date) -> str:
    if (d.month, d.day) == (12, 24):
        return "Christmas Eve"
    if (d.month, d.day) in ((7, 3), (7, 2)):
        return "Day Before Independence Day"
    if d.month == 11 and d.weekday() == 4:
        return "Day After Thanksgiving"
    return "Early Close"


def _collect_full_holidays(
    cal: xc.ExchangeCalendar,
    start_date: date,
    end_date: date,
) -> dict[date, str]:
    full_holidays: dict[date, str] = {}
    reg_holidays = getattr(cal, "regular_holidays", None)
    if reg_holidays is not None:
        h_series = reg_holidays.holidays(
            start_date.isoformat(), end_date.isoformat(), return_name=True
        )
        for dt, name in h_series.items():
            full_holidays[pd.Timestamp(dt).date()] = _clean_holiday_name(str(name))

    for adh in getattr(cal, "adhoc_holidays", []):
        adh_d = adh.date() if isinstance(adh, (pd.Timestamp, datetime)) else adh
        if start_date <= adh_d <= end_date and adh_d not in full_holidays:
            full_holidays[adh_d] = "Special Non-Trading Day"

    cur = start_date
    one_day = timedelta(days=1)
    while cur <= end_date:
        if (
            cur.weekday() < 5
            and not cal.is_session(pd.Timestamp(cur))
            and cur not in full_holidays
        ):
            full_holidays[cur] = "NYSE Holiday"
        cur += one_day

    return full_holidays


def _collect_early_closes(
    cal: xc.ExchangeCalendar,
    start_date: date,
    end_date: date,
) -> dict[date, tuple[str, str]]:
    sched = cal.schedule.loc[start_date.isoformat() : end_date.isoformat()]
    early_closes: dict[date, tuple[str, str]] = {}
    for idx, row in sched.iterrows():
        c = row["close"].tz_convert(ET)
        if c.time() != time(16, 0):
            d = pd.Timestamp(str(idx)).date()
            close_time = c.strftime("%H:%M")
            early_closes[d] = (_get_early_close_name(d), close_time)
    return early_closes


def generate_holiday_csv(
    output_path: Path = DEFAULT_CSV_PATH,
    start_year: int = 2024,
    end_year: int = 2035,
) -> list[dict[str, str]]:
    """Derive NYSE holidays and early-close schedule from exchange_calendars."""
    start_str = f"{start_year - 1}-01-01"
    end_str = f"{end_year + 1}-01-01"
    cal = xc.get_calendar("XNYS", start=start_str, end=end_str)

    start_date = date(start_year, 1, 1)
    end_date = date(end_year, 12, 31)

    full_holidays = _collect_full_holidays(cal, start_date, end_date)
    early_closes = _collect_early_closes(cal, start_date, end_date)

    all_dates = sorted(set(full_holidays.keys()) | set(early_closes.keys()))
    rows: list[dict[str, str]] = []
    for d in all_dates:
        if d in full_holidays:
            rows.append({
                "date": d.isoformat(),
                "holiday_name": full_holidays[d],
                "is_early_close": "false",
                "close_time_et": "",
            })
        elif d in early_closes:
            h_name, close_t = early_closes[d]
            rows.append({
                "date": d.isoformat(),
                "holiday_name": h_name,
                "is_early_close": "true",
                "close_time_et": close_t,
            })

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["date", "holiday_name", "is_early_close", "close_time_et"]
        )
        writer.writeheader()
        writer.writerows(rows)

    return rows


if __name__ == "__main__":
    generated = generate_holiday_csv()
    msg = (
        f"Successfully generated {len(generated)} holiday/early-close entries in "
        f"{DEFAULT_CSV_PATH}"
    )
    print(msg)
