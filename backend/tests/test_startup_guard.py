"""Tests for scripts/holiday_guard.py startup guard."""

import os
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.holiday_guard import check_trading_day


class TestStartupGuard:
    """Validate startup guard gating semantics."""

    def test_normal_trading_day_allows_startup(self):
        # 2026-09-08 is Tuesday trading day -> returns 0
        res = check_trading_day(date(2026, 9, 8))
        assert res == 0

    def test_early_close_allows_startup(self):
        # 2026-11-27 is Black Friday (early close) -> returns 0
        res = check_trading_day(date(2026, 11, 27))
        assert res == 0

    def test_thanksgiving_blocks_startup(self, tmp_path):
        state_file = tmp_path / "state.json"
        with (
            patch.dict(os.environ, {"NOTIFY_STATE_FILE": str(state_file)}),
            patch("scripts.holiday_guard._send_telegram", return_value=True) as mock_tg,
            patch("scripts.holiday_guard._persist_event", return_value=True) as mock_db,
        ):
            res = check_trading_day(date(2026, 11, 26))
            assert res == 1
            mock_tg.assert_called_once()
            mock_db.assert_called_once()
            assert "Thanksgiving" in mock_tg.call_args[0][0]

    def test_saturday_blocks_startup(self, tmp_path):
        state_file = tmp_path / "state.json"
        with (
            patch.dict(os.environ, {"NOTIFY_STATE_FILE": str(state_file)}),
            patch("scripts.holiday_guard._send_telegram", return_value=True) as mock_tg,
            patch("scripts.holiday_guard._persist_event", return_value=True) as mock_db,
        ):
            res = check_trading_day(date(2026, 9, 12))
            assert res == 1
            mock_tg.assert_called_once()
            mock_db.assert_called_once()
            assert "Saturday" in mock_tg.call_args[0][0]

    def test_telegram_failure_still_blocks_startup(self, tmp_path):
        state_file = tmp_path / "state.json"
        with (
            patch.dict(os.environ, {"NOTIFY_STATE_FILE": str(state_file)}),
            patch("scripts.holiday_guard._send_telegram", return_value=False),
            patch("scripts.holiday_guard._persist_event", return_value=True),
        ):
            # Telegram failure must NEVER allow startup
            res = check_trading_day(date(2026, 12, 25))
            assert res == 1

    def test_calendar_error_fails_closed(self):
        with patch("app.services.session_clock.is_trading_day", side_effect=RuntimeError("Calendar crashed")):
            res = check_trading_day(date(2026, 9, 8))
            assert res == 2  # Fail closed

    def test_duplicate_checks_deduplicate_telegram(self, tmp_path):
        state_file = tmp_path / "state.json"
        with (
            patch.dict(os.environ, {"NOTIFY_STATE_FILE": str(state_file)}),
            patch("scripts.holiday_guard._send_telegram", return_value=True) as mock_tg,
            patch("scripts.holiday_guard._persist_event", return_value=True),
        ):
            # First run on holiday
                    res1 = check_trading_day(date(2026, 11, 26))
                    assert res1 == 1
                    assert mock_tg.call_count == 1

                    # Second run on same holiday -> blocked, but telegram not spammed
                    res2 = check_trading_day(date(2026, 11, 26))
                    assert res2 == 1
                    assert mock_tg.call_count == 1  # Still 1
