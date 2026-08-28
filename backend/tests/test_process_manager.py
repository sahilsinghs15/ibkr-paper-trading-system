"""Unit tests for process_manager Gateway-ready gating."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import process_manager as pm


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
