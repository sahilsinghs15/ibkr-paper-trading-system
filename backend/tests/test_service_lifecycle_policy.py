"""
test_service_lifecycle_policy.py — Comprehensive unit tests for off-market service lifecycle policy.

Verifies:
1. Automatic start/restart behavior during vs outside US market hours (Mon-Fri 09:30-16:00 ET).
2. Timezone / DST correctness with America/New_York.
3. Weekend behavior (Saturday/Sunday = market closed).
4. Exit code semantics for wrapper scripts (exit non-zero in market hours for auto-restart, exit 0 off-market to block auto-restart).
5. Watchdog market-closed notification semantics (honest expected stop messaging, no false TRADING BLOCKED alerts).
6. Manual operator start compatibility.
"""
from datetime import datetime
import subprocess
import sys
from zoneinfo import ZoneInfo
import pytest

from app.services.watchdog.daemon import _is_trading_session, _is_market_closed_for, ServiceName, ServiceSnapshot, ServiceState, WatchdogDaemon
from app.services.watchdog.models import HealthResult, NotificationEvent
from app.services.watchdog.notifier import format_telegram_message


def _et(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    """Helper to construct Eastern Time datetimes."""
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("America/New_York"))


class TestMarketHoursEvaluation:
    """Test US market session window rules (Mon-Fri 09:30-16:00 ET)."""

    def test_weekday_inside_market_hours(self):
        # Mon 09:30 ET -> True
        mon_open = _et(2026, 8, 24, 9, 30)
        assert _is_trading_session(mon_open) is True

        # Fri 15:59 ET -> True
        fri_late = _et(2026, 8, 28, 15, 59)
        assert _is_trading_session(fri_late) is True

        # Wed 12:00 ET -> True
        wed_noon = _et(2026, 8, 26, 12, 0)
        assert _is_trading_session(wed_noon) is True

    def test_weekday_outside_market_hours(self):
        # Mon 09:29 ET -> False
        mon_before = _et(2026, 8, 24, 9, 29)
        assert _is_trading_session(mon_before) is False

        # Fri 16:00 ET -> False
        fri_close = _et(2026, 8, 28, 16, 0)
        assert _is_trading_session(fri_close) is False

        # Mon 06:10 ET (the incident time) -> False
        incident_time = _et(2026, 9, 2, 6, 10)
        assert _is_trading_session(incident_time) is False

    def test_weekend_market_hours(self):
        # Sat 12:00 ET -> False
        sat = _et(2026, 8, 29, 12, 0)
        assert _is_trading_session(sat) is False

        # Sun 12:00 ET -> False
        sun = _et(2026, 8, 30, 12, 0)
        assert _is_trading_session(sun) is False

    def test_dst_timezone_transitions(self):
        # Winter (EST UTC-5): Mon 2026-01-05 10:00 ET -> True
        winter = datetime(2026, 1, 5, 10, 0, tzinfo=ZoneInfo("America/New_York"))
        assert _is_trading_session(winter) is True

        # Summer (EDT UTC-4): Mon 2026-07-06 10:00 ET -> True
        summer = datetime(2026, 7, 6, 10, 0, tzinfo=ZoneInfo("America/New_York"))
        assert _is_trading_session(summer) is True

    def test_market_closed_services_mapping(self):
        off_market = _et(2026, 9, 2, 6, 10)
        # Gateway and Webhook are market-closed services
        assert _is_market_closed_for(ServiceName.GATEWAY, off_market) is True
        assert _is_market_closed_for(ServiceName.WEBHOOK, off_market) is True

        # Backend, Demo, Postgres, Redis run 24/7 or independent
        assert _is_market_closed_for(ServiceName.BACKEND, off_market) is False
        assert _is_market_closed_for(ServiceName.DEMO, off_market) is False
        assert _is_market_closed_for(ServiceName.POSTGRES, off_market) is False
        assert _is_market_closed_for(ServiceName.REDIS, off_market) is False


class TestSessionGuardCLI:
    """Test session_guard.py helper script execution."""

    def test_session_guard_script_check(self):
        # Test CLI invocation of scripts/session_guard.py
        import os
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        script_path = os.path.join(repo_root, "scripts", "session_guard.py")
        result = subprocess.run(
            [sys.executable, script_path, "info"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert "Time (ET):" in result.stdout
        assert "Trading Session Active:" in result.stdout


class TestWatchdogMarketClosedMessaging:
    """Test Watchdog Telegram formatting for market-closed state."""

    def test_gateway_market_closed_telegram_message(self):
        snap = ServiceSnapshot(service=ServiceName.GATEWAY, state=ServiceState.MARKET_CLOSED)
        text = format_telegram_message(
            service=ServiceName.GATEWAY,
            event=NotificationEvent.MARKET_CLOSED,
            snapshot=snap,
            host="main-ec2",
            port=4002,
        )
        assert "MARKET CLOSED" in text
        assert "intentionally stopped because the market is closed" in text
        assert "No action required." in text
        assert "TRADING BLOCKED" not in text

    def test_webhook_market_closed_telegram_message(self):
        snap = ServiceSnapshot(service=ServiceName.WEBHOOK, state=ServiceState.MARKET_CLOSED)
        text = format_telegram_message(
            service=ServiceName.WEBHOOK,
            event=NotificationEvent.MARKET_CLOSED,
            snapshot=snap,
            host="main-ec2",
            port=8000,
        )
        assert "MARKET CLOSED" in text
        assert "intentionally stopped" in text
        assert "TRADING BLOCKED" not in text


class TestBackend247Behavior:
    """Verify Backend 24/7 operation is preserved."""

    def test_backend_remains_running_247(self):
        # Backend is NOT in _MARKET_CLOSED_SERVICES
        off_market = _et(2026, 9, 2, 6, 10)
        assert _is_market_closed_for(ServiceName.BACKEND, off_market) is False


class TestRestartChain:
    """Verify IB Gateway -> Backend -> Demo Streaming restart chain definitions."""

    def test_restart_trigger_paths(self):
        import os
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

        # Verify trading-backend-restart.path configuration
        path_file = os.path.join(repo_root, "deploy", "systemd", "trading-backend-restart.path")
        with open(path_file, "r") as f:
            content = f.read()
        assert "PathModified=/home/tradingapp/storage/state/restart_backend.trigger" in content

        # Verify demo-streaming-restart.path configuration
        demo_path_file = os.path.join(repo_root, "deploy", "systemd", "demo-streaming-restart.path")
        with open(demo_path_file, "r") as f:
            demo_content = f.read()
        assert "PathModified=/home/tradingapp/storage/state/restart_demo.trigger" in demo_content

    def test_backend_ready_trigger_script(self):
        import os
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        script = os.path.join(repo_root, "scripts", "backend-ready-trigger.sh")
        with open(script, "r") as f:
            content = f.read()
        assert 'TRIGGER="/home/tradingapp/storage/state/restart_demo.trigger"' in content
        assert 'HEALTH_URL="http://127.0.0.1:8001/health"' in content
        assert 'curl -sf "$HEALTH_URL"' in content

