"""Production fixes: safety gates, priority queue, persisted budget."""

import asyncio
import json
import pathlib
import tempfile

from unittest.mock import AsyncMock, patch, MagicMock

from app.services.watchdog.config import WatchdogSettings
from app.services.watchdog.models import NotificationEvent, ServiceName, ServiceState, ServiceSnapshot
from app.services.watchdog.notifier import NotificationQueue
from app.services.watchdog.recovery_store import RecoveryBudgetStore
from app.services.watchdog.safety import SafetyGateChecker
from app.services.watchdog.telegram import TelegramClient
from datetime import UTC, datetime, timedelta


# ---- safety gates ----

def test_safety_kill_switch_active_blocks():
    s = WatchdogSettings(backend_host="127.0.0.1", backend_port=8001)
    checker = SafetyGateChecker(s)

    async def _run():
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = [
            {"overall_status": "HEALTHY", "alerts": []},  # system-monitor
            {"accounts": [{"id": 1, "ibkr_account": "U123", "kill_switch_active": True}]},  # config/accounts
            {"accounts": [{"ibkr_account": "U123"}]},  # second fetch for ibkr_accounts
            {"incidents": []},  # baskets for U123
        ]
        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=[
                MagicMock(status_code=200, json=lambda: {"overall_status": "HEALTHY", "alerts": []}),
                MagicMock(status_code=200, json=lambda: {"accounts": [{"id": 1, "ibkr_account": "U123", "kill_switch_active": True}]}),
                MagicMock(status_code=200, json=lambda: {"accounts": [{"ibkr_account": "U123"}]}),
                MagicMock(status_code=200, json=lambda: {"incidents": []}),
            ])
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            result = await checker.check()
            assert result.passed is False
            assert any("kill switch" in f.lower() for f in result.failures)
    asyncio.run(_run())


def test_safety_baskets_critical_blocks():
    s = WatchdogSettings(backend_host="127.0.0.1", backend_port=8001)
    checker = SafetyGateChecker(s)

    async def _run():
        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=[
                MagicMock(status_code=200, json=lambda: {"overall_status": "HEALTHY", "alerts": []}),
                MagicMock(status_code=200, json=lambda: {"accounts": [{"id": 1, "ibkr_account": "U123", "kill_switch_active": False}]}),
                MagicMock(status_code=200, json=lambda: {"accounts": [{"ibkr_account": "U123"}]}),
                MagicMock(status_code=200, json=lambda: {"incidents": [{"basket_id": 1}]}),
            ])
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            result = await checker.check()
            assert result.passed is False
            assert any("BASKET_CRITICAL" in f for f in result.failures)

    asyncio.run(_run())


def test_safety_all_gates_healthy_passes():
    s = WatchdogSettings(backend_host="127.0.0.1", backend_port=8001)
    checker = SafetyGateChecker(s)

    async def _run():
        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=[
                MagicMock(status_code=200, json=lambda: {"overall_status": "HEALTHY", "alerts": []}),
                MagicMock(status_code=200, json=lambda: {"accounts": [{"id": 1, "ibkr_account": "U123", "kill_switch_active": False}]}),
                MagicMock(status_code=200, json=lambda: {"accounts": [{"ibkr_account": "U123"}]}),
                MagicMock(status_code=200, json=lambda: {"incidents": []}),
            ])
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            result = await checker.check()
            assert result.passed is True

    asyncio.run(_run())


# ---- priority queue ----

def test_critical_not_evicted_by_info():
    s = WatchdogSettings(telegram_enabled=False)
    tg = TelegramClient(None, None, enabled=False)
    q = NotificationQueue(tg, s)
    # fill with info
    for i in range(100):
        q.enqueue(ServiceName.DEMO, NotificationEvent.RECOVERED, f"info {i}", force=True)
    assert q._total() == 100
    # now critical should evict info, not be dropped
    ok = q.enqueue(ServiceName.GATEWAY, NotificationEvent.FAILURE, "critical", force=True)
    assert ok is True
    assert len(q._critical) == 1
    assert q._total() == 100
    # ensure critical is present
    assert any(e == NotificationEvent.FAILURE for _, e, _ in q._critical)


def test_warning_cannot_evict_critical():
    s = WatchdogSettings(telegram_enabled=False)
    tg = TelegramClient(None, None, enabled=False)
    q = NotificationQueue(tg, s)
    for i in range(100):
        q.enqueue(ServiceName.GATEWAY, NotificationEvent.FAILURE, f"crit {i}", force=True)
    assert len(q._critical) == 100
    ok = q.enqueue(ServiceName.BACKEND, NotificationEvent.UNHEALTHY, "warning", force=True)
    # warning should be dropped because critical reserved
    assert ok is False


# ---- recovery persistence ----

def test_recovery_persist_and_reload():
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "watchdog_recovery.json"
        s = WatchdogSettings(recovery_state_path=str(path), recovery_max_attempts=5, recovery_window_seconds=600)
        store = RecoveryBudgetStore(s)
        for _ in range(5):
            store.add_attempt("backend")
        assert store.is_exhausted("backend", 5, 600) is True
        # simulate restart: new store instance loads same file
        s2 = WatchdogSettings(recovery_state_path=str(path), recovery_max_attempts=5, recovery_window_seconds=600)
        store2 = RecoveryBudgetStore(s2)
        assert store2.is_exhausted("backend", 5, 600) is True


def test_recovery_window_expiry():
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "watchdog_recovery.json"
        s = WatchdogSettings(recovery_state_path=str(path), recovery_max_attempts=5, recovery_window_seconds=10)
        store = RecoveryBudgetStore(s)
        old = datetime.now(UTC) - timedelta(seconds=20)
        state = {"backend": [old.isoformat(), old.isoformat()]}
        store.save(state)
        assert store.is_exhausted("backend", 5, 10) is False


def test_recovery_corrupted_file_fail_closed():
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "watchdog_recovery.json"
        path.write_text("{ not json")
        s = WatchdogSettings(recovery_state_path=str(path))
        store = RecoveryBudgetStore(s)
        store.load()
        assert store.is_corrupted() is True
        assert store.is_exhausted("backend", 5, 600) is True


def test_recovery_future_timestamp_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "watchdog_recovery.json"
        future = datetime.now(UTC) + timedelta(days=1)
        data = {"backend": [future.isoformat()]}
        path.write_text(json.dumps(data))
        s = WatchdogSettings(recovery_state_path=str(path))
        store = RecoveryBudgetStore(s)
        attempts = store.get_attempts("backend")
        # future should be filtered out
        assert len(attempts) == 0


def test_recovery_atomic_write():
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "watchdog_recovery.json"
        s = WatchdogSettings(recovery_state_path=str(path))
        store = RecoveryBudgetStore(s)
        store.save({"backend": [datetime.now(UTC).isoformat()]} )
        store.save({"backend": [datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()]} )
        data = json.loads(path.read_text())
        assert "backend" in data
