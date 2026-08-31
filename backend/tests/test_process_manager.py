"""Unit tests for process_manager Gateway-ready gating."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import os
import tempfile
_tmp_log_dir = tempfile.mkdtemp()
os.environ["STORAGE_LOG_ROOT"] = _tmp_log_dir

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import process_manager as pm

ET = ZoneInfo("America/New_York")


def _et(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ET)


class TestDatedLogDir:
    def test_dated_log_dir_under_storage_root(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(pm, "STORAGE_LOG_ROOT", tmp_path)
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        path = pm.dated_log_dir()
        assert path == tmp_path / today
        assert path.is_dir()

    def test_gateway_log_path_is_in_date_folder(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(pm, "STORAGE_LOG_ROOT", tmp_path)
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        path = pm.gateway_log_path()
        assert path == tmp_path / today / "ib_gateway.log"


class TestLogContainsSince:
    def test_missing_file_is_not_ready(self, tmp_path: Path) -> None:
        assert pm.log_contains_since(tmp_path / "missing.log", pm.GATEWAY_LOGIN_MARKER, 0) is False

    def test_ignores_login_before_offset(self, tmp_path: Path) -> None:
        path = tmp_path / "ib_gateway.log"
        path.write_text("2026-08-27 09:00:00 IBC: Login has completed\n")
        offset = path.stat().st_size
        path.write_text(path.read_text() + "still authenticating\n")
        assert pm.log_contains_since(path, pm.GATEWAY_LOGIN_MARKER, offset) is False

    def test_detects_login_after_offset(self, tmp_path: Path) -> None:
        path = tmp_path / "ib_gateway.log"
        path.write_text("previous session\n")
        offset = path.stat().st_size
        path.write_text(path.read_text() + "2026-08-27 09:33:35 IBC: Login has completed\n")
        assert pm.log_contains_since(path, pm.GATEWAY_LOGIN_MARKER, offset) is True

    def test_log_file_size_missing(self, tmp_path: Path) -> None:
        assert pm.log_file_size(tmp_path / "nope.log") == 0


class TestWaitForGatewayReady:
    def test_ready_when_login_and_port(self, tmp_path: Path) -> None:
        path = tmp_path / "ib_gateway.log"
        path.write_text("2026-08-27 09:33:35 IBC: Login has completed\n")
        ok = pm.wait_for_gateway_ready(
            log_path=path,
            log_offset=0,
            is_alive=lambda: True,
            port_check=lambda: True,
            timeout_sec=1,
            poll_sec=0.01,
            settle_sec=0,
        )
        assert ok is True

    def test_not_ready_without_login_even_if_port_is_up(self, tmp_path: Path) -> None:
        path = tmp_path / "ib_gateway.log"
        path.write_text("Authenticating...\n")
        ok = pm.wait_for_gateway_ready(
            log_path=path,
            log_offset=0,
            is_alive=lambda: True,
            port_check=lambda: True,
            timeout_sec=0.05,
            poll_sec=0.01,
            settle_sec=0,
        )
        assert ok is False

    def test_not_ready_without_port_even_if_logged_in(self, tmp_path: Path) -> None:
        path = tmp_path / "ib_gateway.log"
        path.write_text("IBC: Login has completed\n")
        ok = pm.wait_for_gateway_ready(
            log_path=path,
            log_offset=0,
            is_alive=lambda: True,
            port_check=lambda: False,
            timeout_sec=0.05,
            poll_sec=0.01,
            settle_sec=0,
        )
        assert ok is False

    def test_stale_login_before_offset_is_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "ib_gateway.log"
        path.write_text("IBC: Login has completed\n")
        offset = path.stat().st_size
        path.write_text(path.read_text() + "Authenticating (trying for another 18 seconds)...\n")
        ok = pm.wait_for_gateway_ready(
            log_path=path,
            log_offset=offset,
            is_alive=lambda: True,
            port_check=lambda: True,
            timeout_sec=0.05,
            poll_sec=0.01,
            settle_sec=0,
        )
        assert ok is False

    def test_aborts_when_process_dies(self, tmp_path: Path) -> None:
        path = tmp_path / "ib_gateway.log"
        path.write_text("")
        ok = pm.wait_for_gateway_ready(
            log_path=path,
            log_offset=0,
            is_alive=lambda: False,
            port_check=lambda: True,
            timeout_sec=1,
            poll_sec=0.01,
            settle_sec=0,
        )
        assert ok is False

    def test_aborts_on_shutdown(self, tmp_path: Path) -> None:
        path = tmp_path / "ib_gateway.log"
        path.write_text("IBC: Login has completed\n")
        ok = pm.wait_for_gateway_ready(
            log_path=path,
            log_offset=0,
            is_alive=lambda: True,
            port_check=lambda: True,
            should_abort=lambda: True,
            timeout_sec=1,
            poll_sec=0.01,
            settle_sec=0,
        )
        assert ok is False


class TestProcessCommands:
    def test_webhook_ingest_cmd_uses_port_8000(self) -> None:
        cmd = pm.webhook_ingest_cmd()
        assert "app.webhook_ingest:app" in cmd
        assert str(pm.INGEST_PORT) in cmd

    def test_fastapi_cmd_uses_port_8001(self) -> None:
        cmd = pm.fastapi_cmd()
        assert "app.main:app" in cmd
        assert str(pm.TRADING_PORT) in cmd

    def test_ingest_and_trading_ports_differ(self) -> None:
        assert pm.INGEST_PORT == 8000
        assert pm.TRADING_PORT == 8001
        assert pm.INGEST_PORT != pm.TRADING_PORT


class TestIsTradingSession:
    def test_monday_open_at_930(self) -> None:
        assert pm.is_trading_session(_et(2026, 8, 24, 9, 30)) is True

    def test_monday_closed_at_929(self) -> None:
        assert pm.is_trading_session(_et(2026, 8, 24, 9, 29)) is False

    def test_friday_open_at_1559(self) -> None:
        assert pm.is_trading_session(_et(2026, 8, 28, 15, 59)) is True

    def test_friday_closed_at_1600(self) -> None:
        assert pm.is_trading_session(_et(2026, 8, 28, 16, 0)) is False

    def test_saturday_noon_closed(self) -> None:
        assert pm.is_trading_session(_et(2026, 8, 29, 12, 0)) is False

    def test_sunday_noon_closed(self) -> None:
        assert pm.is_trading_session(_et(2026, 8, 30, 12, 0)) is False


class TestResolveEnabledGroups:
    def test_default_all(self) -> None:
        assert pm.resolve_enabled_groups([]) == pm.ALL_GROUPS

    def test_webhook_only(self) -> None:
        assert pm.resolve_enabled_groups(["webhook"]) == frozenset({"webhook"})

    def test_fastapi_implies_gateway(self) -> None:
        assert pm.resolve_enabled_groups(["fastapi"]) == frozenset(
            {"gateway", "fastapi"}
        )

    def test_gateway_only(self) -> None:
        assert pm.resolve_enabled_groups(["gateway"]) == frozenset({"gateway"})

    def test_webhook_and_fastapi(self) -> None:
        assert pm.resolve_enabled_groups(["webhook", "fastapi"]) == frozenset(
            {"webhook", "gateway", "fastapi"}
        )

    def test_unknown_group_raises(self) -> None:
        with pytest.raises(ValueError, match="nope"):
            pm.resolve_enabled_groups(["nope"])


class TestParseCliGroups:
    def test_empty_argv_is_all(self) -> None:
        assert pm.parse_cli_groups([]) == pm.ALL_GROUPS

    def test_unknown_group_exits(self) -> None:
        with pytest.raises(SystemExit):
            pm.parse_cli_groups(["nope"])


class TestEnsureDemoStreamingReconnected:
    def test_fastapi_disabled_does_nothing(self, monkeypatch) -> None:
        sup = pm.Supervisor(enabled=frozenset({"webhook"}))
        called = False

        def mock_run(*args, **kwargs):
            nonlocal called
            called = True

        monkeypatch.setattr(pm.subprocess, "run", mock_run)
        sup._ensure_demo_streaming_reconnected()
        assert called is False

    def test_fastapi_dead_does_nothing(self, monkeypatch) -> None:
        sup = pm.Supervisor(enabled=frozenset({"fastapi"}))
        monkeypatch.setattr(sup.fastapi, "is_alive", lambda: False)
        called = False

        def mock_run(*args, **kwargs):
            nonlocal called
            called = True

        monkeypatch.setattr(pm.subprocess, "run", mock_run)
        sup._ensure_demo_streaming_reconnected()
        assert called is False

    def test_fastapi_unhealthy_does_nothing(self, monkeypatch) -> None:
        sup = pm.Supervisor(enabled=frozenset({"fastapi"}))
        monkeypatch.setattr(sup.fastapi, "is_alive", lambda: True)
        monkeypatch.setattr(pm, "fastapi_healthy", lambda: False)
        called = False

        def mock_run(*args, **kwargs):
            nonlocal called
            called = True

        monkeypatch.setattr(pm.subprocess, "run", mock_run)
        sup._ensure_demo_streaming_reconnected()
        assert called is False

    def test_restarts_on_new_healthy_epoch(self, monkeypatch) -> None:
        sup = pm.Supervisor(enabled=frozenset({"fastapi"}))
        sup._fastapi_epoch = 1
        sup._demo_restarted_epoch = 0
        monkeypatch.setattr(sup.fastapi, "is_alive", lambda: True)
        monkeypatch.setattr(pm, "fastapi_healthy", lambda: True)

        cmd_run = []

        class MockCompletedProcess:
            returncode = 0
            stderr = ""

        def mock_run(cmd, **kwargs):
            cmd_run.append(cmd)
            return MockCompletedProcess()

        monkeypatch.setattr(pm.subprocess, "run", mock_run)
        sup._ensure_demo_streaming_reconnected()

        assert cmd_run == [["sudo", "/usr/bin/systemctl", "restart", "demo-streaming.service"]]
        assert sup._demo_restarted_epoch == 1

        # Second invocation in same epoch does nothing
        cmd_run.clear()
        sup._ensure_demo_streaming_reconnected()
        assert cmd_run == []
        assert sup._demo_restarted_epoch == 1

    def test_failed_restart_does_not_update_epoch(self, monkeypatch) -> None:
        sup = pm.Supervisor(enabled=frozenset({"fastapi"}))
        sup._fastapi_epoch = 1
        sup._demo_restarted_epoch = 0
        monkeypatch.setattr(sup.fastapi, "is_alive", lambda: True)
        monkeypatch.setattr(pm, "fastapi_healthy", lambda: True)

        class MockFailedProcess:
            returncode = 1
            stderr = "Permission denied"

        monkeypatch.setattr(pm.subprocess, "run", lambda cmd, **kwargs: MockFailedProcess())
        sup._ensure_demo_streaming_reconnected()

        assert sup._demo_restarted_epoch == 0

    def test_exception_does_not_crash(self, monkeypatch) -> None:
        sup = pm.Supervisor(enabled=frozenset({"fastapi"}))
        sup._fastapi_epoch = 1
        sup._demo_restarted_epoch = 0
        monkeypatch.setattr(sup.fastapi, "is_alive", lambda: True)
        monkeypatch.setattr(pm, "fastapi_healthy", lambda: True)

        def mock_raise(*args, **kwargs):
            raise TimeoutError("Command timed out")

        monkeypatch.setattr(pm.subprocess, "run", mock_raise)
        # Must not raise exception
        sup._ensure_demo_streaming_reconnected()
        assert sup._demo_restarted_epoch == 0

