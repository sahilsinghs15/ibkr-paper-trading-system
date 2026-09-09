"""Tests for the canonical NYSE holiday calendar, CSV generation, and fallback behavior."""

import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.generate_holiday_csv import generate_holiday_csv

from app.services.session_clock import (
    get_holiday_reason,
    is_trading_day,
    rth_close_for,
    rth_open_for,
)

ET = ZoneInfo("America/New_York")


class TestNyseCalendar:
    """Validate canonical XNYS calendar decisions."""

    def test_normal_trading_day(self):
        # Tuesday 2026-09-08 is a standard trading day
        d = date(2026, 9, 8)
        assert is_trading_day(d) is True
        assert get_holiday_reason(d) == ""

    def test_saturday(self):
        d = date(2026, 9, 12)
        assert is_trading_day(d) is False
        assert get_holiday_reason(d) == "Saturday"

    def test_sunday(self):
        d = date(2026, 9, 13)
        assert is_trading_day(d) is False
        assert get_holiday_reason(d) == "Sunday"

    def test_fixed_holiday_christmas(self):
        # 2026-12-25 is Friday Christmas
        d = date(2026, 12, 25)
        assert is_trading_day(d) is False
        assert "Christmas" in get_holiday_reason(d)

    def test_floating_holiday_thanksgiving(self):
        # 2026-11-26 is Thanksgiving (4th Thursday in Nov)
        d = date(2026, 11, 26)
        assert is_trading_day(d) is False
        assert "Thanksgiving" in get_holiday_reason(d)

    def test_floating_holiday_mlk(self):
        # 2026-01-19 is Martin Luther King Jr. Day (3rd Monday in Jan)
        d = date(2026, 1, 19)
        assert is_trading_day(d) is False
        assert "King" in get_holiday_reason(d)

    def test_floating_holiday_presidents_day(self):
        # 2026-02-16 is Presidents' Day (3rd Monday in Feb)
        d = date(2026, 2, 16)
        assert is_trading_day(d) is False
        assert "President" in get_holiday_reason(d)

    def test_floating_holiday_good_friday(self):
        # 2026-04-03 is Good Friday
        d = date(2026, 4, 3)
        assert is_trading_day(d) is False
        assert "Good Friday" in get_holiday_reason(d)

    def test_floating_holiday_memorial_day(self):
        # 2026-05-25 is Memorial Day
        d = date(2026, 5, 25)
        assert is_trading_day(d) is False
        assert "Memorial Day" in get_holiday_reason(d)

    def test_juneteenth(self):
        # 2026-06-19 is Juneteenth
        d = date(2026, 6, 19)
        assert is_trading_day(d) is False
        assert "Juneteenth" in get_holiday_reason(d)

    def test_labor_day(self):
        # 2026-09-07 is Labor Day (1st Monday in Sep)
        d = date(2026, 9, 7)
        assert is_trading_day(d) is False
        assert "Labor Day" in get_holiday_reason(d)

    def test_observed_holiday_independence_day(self):
        # 2026-07-04 is Saturday -> observed on Friday 2026-07-03
        d = date(2026, 7, 3)
        assert is_trading_day(d) is False
        assert "Independence" in get_holiday_reason(d)

    def test_early_close_black_friday(self):
        # 2026-11-27 is Day After Thanksgiving (early close 13:00)
        d = date(2026, 11, 27)
        assert is_trading_day(d) is True
        c = rth_close_for(d)
        assert c is not None
        assert c.hour == 13
        assert c.minute == 0

    def test_early_close_christmas_eve(self):
        # 2026-12-24 is Christmas Eve (early close 13:00)
        d = date(2026, 12, 24)
        assert is_trading_day(d) is True
        c = rth_close_for(d)
        assert c is not None
        assert c.hour == 13
        assert c.minute == 0

    def test_dst_spring_and_fall(self):
        # DST spring 2026-03-08 clocks forward
        mon_spring = date(2026, 3, 9)
        assert is_trading_day(mon_spring) is True
        o = rth_open_for(mon_spring)
        assert o is not None
        assert o.hour == 9 and o.minute == 30

        # DST fall 2026-11-01 clocks back
        mon_fall = date(2026, 11, 2)
        assert is_trading_day(mon_fall) is True
        o = rth_open_for(mon_fall)
        assert o is not None
        assert o.hour == 9 and o.minute == 30

    def test_future_year_2030(self):
        # 2030 Christmas is Wednesday 2030-12-25
        xmas_2030 = date(2030, 12, 25)
        assert is_trading_day(xmas_2030) is False
        assert "Christmas" in get_holiday_reason(xmas_2030)

        # 2030 normal day: Monday 2030-09-09
        mon_2030 = date(2030, 9, 9)
        assert is_trading_day(mon_2030) is True

    def test_csv_reproducibility(self, tmp_path):
        csv_file = tmp_path / "test_holidays.csv"
        rows = generate_holiday_csv(output_path=csv_file, start_year=2024, end_year=2026)
        assert len(rows) > 0
        dates = [r["date"] for r in rows]
        assert "2026-11-26" in dates  # Thanksgiving
        assert "2026-11-27" in dates  # Black Friday (early close)

        bf = next(r for r in rows if r["date"] == "2026-11-27")
        assert bf["is_early_close"] == "true"
        assert bf["close_time_et"] == "13:00"

    def test_csv_fallback_when_calendar_unavailable(self):
        # Simulate calendar unavailable by patching _cal to return None
        with patch("app.services.session_clock._cal", return_value=None):
            # Thanksgiving 2026 from CSV fallback
            d = date(2026, 11, 26)
            assert is_trading_day(d) is False
            assert "Thanksgiving" in get_holiday_reason(d)

            # Black Friday 2026 from CSV fallback
            bf = date(2026, 11, 27)
            assert is_trading_day(bf) is True
            c = rth_close_for(bf)
            assert c is not None
            assert c.hour == 13

            # Normal day within CSV range: 2026-09-08 (Tuesday)
            norm = date(2026, 9, 8)
            assert is_trading_day(norm) is True

            # Weekend within CSV range
            sat = date(2026, 9, 12)
            assert is_trading_day(sat) is False
            assert get_holiday_reason(sat) == "Saturday"

    def test_fail_closed_when_both_unavailable(self):
        # Simulate both calendar and CSV unavailable
        with (
            patch("app.services.session_clock._cal", return_value=None),
            patch("app.services.session_clock._load_csv_schedule", return_value=({}, None)),
        ):
            # Must NOT fail open to weekday < 5!
            # Even for a Wednesday, it must return False (fail closed)
            wed = date(2026, 9, 9)
            assert is_trading_day(wed) is False
