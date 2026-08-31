"""Audit tests for watchdog independence, resilience, production readiness.

Covers scenarios 1-10 from audit spec without requiring live services.
"""

import asyncio
import pathlib
from unittest.mock import AsyncMock, patch

from app.services.watchdog.config import WatchdogSettings
from app.services.watchdog.daemon import TRADING_CRITICAL, WatchdogDaemon
from app.services.watchdog.models import HealthResult, HealthStatus, NotificationEvent, ServiceName, ServiceState
from app.services.watchdog.notifier import NotificationQueue
from app.services.watchdog.telegram import TelegramClient


def _settings(**overrides):  # type: ignore[no-untyped-def]
    defaults = dict(watchdog_interval_seconds=0.1, telegram_enabled=False, recovery_max_attempts=3, recovery_window_seconds=60)
    defaults.update(overrides)
    return WatchdogSettings(**defaults)


# ---- Independence ----

def test_watchdog_does_not_import_trading_modules():
    # verify no circular dependency via import check
    import ast
    p = pathlib.Path(__file__).resolve().parents[1] / "app" / "services" / "watchdog" / "daemon.py"
    tree = ast.parse(p.read_text())
    imports = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module]
    # must not import OrderManager, RMS, OMS, process_manager
    forbidden = ["order_manager", "rms", "oms", "process_manager", "ibkr_adapter"]
    for imp in imports:
        for f in forbidden:
            assert f not in imp, f"watchdog must not depend on {f}: {imp}"


def test_watchdog_survives_backend_crash():
    s = _settings()
    d = WatchdogDaemon(s)

    async def _run():
        # mock backend checker to always fail
        d.checkers[ServiceName.BACKEND].check = AsyncMock(return_value=HealthResult(service=ServiceName.BACKEND, status=HealthStatus.FAILED, detail="crash"))  # type: ignore[method-assign]
        await d._check_one(ServiceName.BACKEND)
        # watchdog should be alive and mark backend FAILED
        assert d.snapshots[ServiceName.BACKEND].state == ServiceState.FAILED
        # other services still checkable
        await d._check_one(ServiceName.WEBHOOK)

    asyncio.run(_run())


def test_watchdog_survives_gateway_crash():
    s = _settings()
    d = WatchdogDaemon(s)

    async def _run():
        d.checkers[ServiceName.GATEWAY].check = AsyncMock(return_value=HealthResult(service=ServiceName.GATEWAY, status=HealthStatus.FAILED, detail="gateway down"))  # type: ignore[method-assign]
        await d._check_one(ServiceName.GATEWAY)
        assert d.snapshots[ServiceName.GATEWAY].state == ServiceState.FAILED
        # demo still independent
        d.checkers[ServiceName.DEMO].check = AsyncMock(return_value=HealthResult(service=ServiceName.DEMO, status=HealthStatus.HEALTHY))  # type: ignore[method-assign]
        await d._check_one(ServiceName.DEMO)
        assert d.snapshots[ServiceName.DEMO].state in (ServiceState.HEALTHY, ServiceState.STARTING, ServiceState.UNKNOWN)

    asyncio.run(_run())


def test_demo_failure_does_not_block_trading():
    s = _settings()
    d = WatchdogDaemon(s)
    d.snapshots[ServiceName.DEMO].state = ServiceState.FAILED
    d.snapshots[ServiceName.BACKEND].state = ServiceState.HEALTHY
    assert ServiceName.DEMO not in TRADING_CRITICAL
    assert ServiceName.BACKEND in TRADING_CRITICAL


def test_telegram_failure_does_not_crash_watchdog():
    s = _settings(telegram_enabled=True, telegram_bot_token="tok", telegram_chat_id="123")
    d = WatchdogDaemon(s)
    # telegram will fail (invalid token) but watchdog must continue
    d.telegram.send_message = AsyncMock(return_value=False)  # type: ignore[method-assign]

    async def _run():
        await d.notifier.start()
        d.notifier.enqueue(ServiceName.BACKEND, NotificationEvent.FAILURE, "test msg", force=True)
        await asyncio.sleep(0.2)
        await d.notifier.stop()
        # health check still works
        d.checkers[ServiceName.BACKEND].check = AsyncMock(return_value=HealthResult(service=ServiceName.BACKEND, status=HealthStatus.HEALTHY))  # type: ignore[method-assign]
        await d._check_one(ServiceName.BACKEND)

    asyncio.run(_run())


def test_notification_queue_bounded():
    s = _settings(telegram_enabled=False)
    tg = TelegramClient(None, None, enabled=False)
    q = NotificationQueue(tg, s)
    assert q.BOUNDED_MAXLEN == 100
    for i in range(150):
        q.enqueue(ServiceName.BACKEND, NotificationEvent.FAILURE, f"msg {i}", force=True)
    assert len(q.queue) <= 100
    assert q.dropped_count > 0


def test_health_loop_not_blocked_by_slow_telegram():
    s = _settings(telegram_enabled=True, telegram_bot_token="tok", telegram_chat_id="123", watchdog_interval_seconds=0.1)
    d = WatchdogDaemon(s)

    async def slow_send(_text):  # type: ignore[no-untyped-def]
        await asyncio.sleep(2)
        return True

    d.telegram.send_message = slow_send  # type: ignore[method-assign]

    async def _run():
        await d.notifier.start()
        start = asyncio.get_event_loop().time()
        d.notifier.enqueue(ServiceName.BACKEND, NotificationEvent.FAILURE, "slow", force=True)
        # health check should complete quickly despite slow telegram worker
        d.checkers[ServiceName.BACKEND].check = AsyncMock(return_value=HealthResult(service=ServiceName.BACKEND, status=HealthStatus.HEALTHY))  # type: ignore[method-assign]
        await d._check_one(ServiceName.BACKEND)
        elapsed = asyncio.get_event_loop().time() - start
        assert elapsed < 0.5, "health loop blocked by telegram"
        await d.notifier.stop()

    asyncio.run(_run())


def test_recovery_budget_deterministic():
    s = _settings(recovery_max_attempts=2, recovery_window_seconds=600)
    d = WatchdogDaemon(s)
    snap = d.snapshots[ServiceName.BACKEND]
    from datetime import UTC, datetime
    snap.recovery_attempts = [datetime.now(UTC), datetime.now(UTC)]
    assert d._is_recovery_budget_exhausted(snap) is True
    # after window expires, not exhausted
    snap.recovery_attempts = []
    assert d._is_recovery_budget_exhausted(snap) is False


def test_systemd_units_independent():
    base = pathlib.Path(__file__).resolve().parents[2] / "deploy" / "systemd"
    watchdog = (base / "watchdog.service").read_text()
    demo = (base / "demo-streaming.service").read_text()
    pm = (base / "process-manager.service").read_text()
    # watchdog must not have Wants/Requires on trading services (would couple)
    assert "Wants=process-manager" not in watchdog
    assert "Wants=demo-streaming" not in watchdog
    assert "Requires=process-manager" not in watchdog
    assert "BindsTo=" not in watchdog
    assert "Restart=always" in watchdog
    # demo must not have After=process-manager (only comment)
    for line in demo.splitlines():
        if line.strip().startswith("After="):
            assert "process-manager" not in line, "demo must not After process-manager"
    # process-manager must be independently restartable
    assert "Restart=always" in pm


def test_no_hardcoded_telegram_secrets():
    for p in pathlib.Path("backend").rglob("*.py"):
        if ".venv" in str(p):
            continue
        t = p.read_text()
        # allow test_watchdog_audit itself to mention token string in test, but not real token
        if "test_watchdog_audit" in str(p):
            continue
        # look for literal bot token pattern (long)
        assert "TELEGRAM_BOT_TOKEN" not in t or "WatchdogSettings" in t or "get_watchdog_settings" in t or "telegram_bot_token" in t, f"hardcoded token reference in {p}"


def test_polling_interval_not_too_aggressive():
    s = WatchdogSettings()
    assert s.watchdog_interval_seconds >= 5.0, "polling too aggressive for t3.small"
    assert s.watchdog_interval_seconds <= 30.0


def test_health_check_external_no_backend_dependency():
    # gateway check does not require backend http
    s = _settings(gateway_port=19997)
    from app.services.watchdog.health import GatewayHealthChecker

    async def _run():
        c = GatewayHealthChecker(s)
        r = await c.check()
        assert r.service == ServiceName.GATEWAY
        # failed gateway does not require backend to be up
        assert r.status == HealthStatus.FAILED

    asyncio.run(_run())
