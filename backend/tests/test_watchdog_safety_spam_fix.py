"""Unit tests for Watchdog safety gate flapping fix and service health separation."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.watchdog.config import WatchdogSettings
from app.services.watchdog.daemon import WatchdogDaemon
from app.services.watchdog.models import (
    HealthResult,
    HealthStatus,
    ServiceName,
    ServiceState,
)
from app.services.watchdog.safety import SafetyGateChecker


def test_ram_critical_produces_unsafe_never_unknown():
    """Test A: RAM critical alert produces system_monitor = UNSAFE (never UNKNOWN)."""
    settings = WatchdogSettings(backend_host="127.0.0.1", backend_port=8001)
    checker = SafetyGateChecker(settings)

    async def _run():
        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=[
                # Gate 1: system-monitor (RAM critical)
                MagicMock(
                    status_code=200,
                    json=lambda: {
                        "overall_status": "CRITICAL",
                        "alerts": [
                            {"level": "CRITICAL", "component": "RAM", "message": "RAM usage critical (90.6%)"}
                        ],
                    },
                ),
                # Gate 2: kill switch
                MagicMock(status_code=200, json=lambda: {"accounts": [{"id": 1, "ibkr_account": "U123", "kill_switch_active": False}]}),
                # Gate 3: baskets
                MagicMock(status_code=200, json=lambda: {"accounts": [{"ibkr_account": "U123"}]}),
                MagicMock(status_code=200, json=lambda: {"incidents": []}),
                MagicMock(status_code=200, json=lambda: {"accounts": []}),
            ])
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)

            res = await checker.check()
            assert res.passed is False
            assert res.gates["system_monitor"] == "UNSAFE"
            assert res.gates["system_monitor"] != "UNKNOWN"
            assert any("RAM usage critical (90.6%)" in f for f in res.failures)

    asyncio.run(_run())


def test_healthy_postgres_with_unsafe_system_monitor():
    """Test B: Healthy PostgreSQL connection retains HEALTHY service state when system-monitor is UNSAFE."""
    settings = WatchdogSettings(backend_host="127.0.0.1", backend_port=8001)
    daemon = WatchdogDaemon(settings)

    # Postgres health check passes (SELECT 1 -> OK)
    hr = HealthResult(
        service=ServiceName.POSTGRES,
        status=HealthStatus.HEALTHY,
        detail="Connected to ibkr_trading (SELECT 1 -> OK)",
        reason="healthy",
    )
    daemon.checkers[ServiceName.POSTGRES].check = AsyncMock(return_value=hr)

    # Mock safety check returning UNSAFE due to RAM
    unsafe_gate = MagicMock(
        passed=False,
        failures=["system-monitor CRITICAL [RAM]: RAM usage critical (90.6%)"],
        gates={"system_monitor": "UNSAFE", "kill_switch": "SAFE", "baskets": "SAFE", "trading_mode": "SAFE"},
    )
    daemon.safety.check = AsyncMock(return_value=unsafe_gate)

    async def _run():
        await daemon._check_one(ServiceName.POSTGRES)
        snap = daemon.snapshots[ServiceName.POSTGRES]
        # PostgreSQL service state must remain HEALTHY (not mutated to TRADING_BLOCKED)
        assert snap.state == ServiceState.HEALTHY
        assert snap.last_health.status == HealthStatus.HEALTHY

    asyncio.run(_run())


def test_healthy_backend_with_unsafe_system_monitor():
    """Test C: Healthy Trading Backend retains HEALTHY service state when system-monitor is UNSAFE."""
    settings = WatchdogSettings(backend_host="127.0.0.1", backend_port=8001)
    daemon = WatchdogDaemon(settings)

    hr = HealthResult(
        service=ServiceName.BACKEND,
        status=HealthStatus.HEALTHY,
        detail="HTTP 200 -> OK",
        reason="healthy",
    )
    daemon.checkers[ServiceName.BACKEND].check = AsyncMock(return_value=hr)

    unsafe_gate = MagicMock(
        passed=False,
        failures=["system-monitor CRITICAL [RAM]: RAM usage critical (90.6%)"],
        gates={"system_monitor": "UNSAFE", "kill_switch": "SAFE", "baskets": "SAFE", "trading_mode": "SAFE"},
    )
    daemon.safety.check = AsyncMock(return_value=unsafe_gate)

    async def _run():
        await daemon._check_one(ServiceName.BACKEND)
        snap = daemon.snapshots[ServiceName.BACKEND]
        assert snap.state == ServiceState.HEALTHY

    asyncio.run(_run())


def test_safety_gate_recovery_notification():
    """Test D: Safety gate recovery emits TRADING SAFETY CLEARED notification, not false PostgreSQL recovery."""
    settings = WatchdogSettings(backend_host="127.0.0.1", backend_port=8001)
    daemon = WatchdogDaemon(settings)
    daemon.notifier.enqueue = MagicMock()

    # Mock all health checkers as healthy
    for svc in daemon.checkers:
        daemon.checkers[svc].check = AsyncMock(
            return_value=HealthResult(service=svc, status=HealthStatus.HEALTHY, detail="OK")
        )

    # 1. Unsafe safety gate
    unsafe_gate = MagicMock(
        passed=False,
        failures=["system-monitor CRITICAL [RAM]: RAM usage critical (90.6%)"],
        details="system-monitor CRITICAL [RAM]: RAM usage critical (90.6%)",
    )
    daemon.safety.check = AsyncMock(return_value=unsafe_gate)

    async def _run():
        with patch("app.services.watchdog.daemon._is_trading_session", return_value=True):
            await daemon._check_services()

            # Verify PostgreSQL state remained HEALTHY
            assert daemon.snapshots[ServiceName.POSTGRES].state == ServiceState.HEALTHY

            # 2. Safety gate recovers to SAFE
            safe_gate = MagicMock(passed=True, failures=[], details="all gates SAFE")
            daemon.safety.check = AsyncMock(return_value=safe_gate)

            await daemon._check_services()

            # Verify PostgreSQL remained HEALTHY throughout
            assert daemon.snapshots[ServiceName.POSTGRES].state == ServiceState.HEALTHY

    asyncio.run(_run())


def test_genuine_postgres_critical_system_monitor_alert():
    """Test E: Genuine PostgreSQL critical system monitor alert fails closed with UNSAFE."""
    settings = WatchdogSettings(backend_host="127.0.0.1", backend_port=8001)
    checker = SafetyGateChecker(settings)

    async def _run():
        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=[
                MagicMock(
                    status_code=200,
                    json=lambda: {
                        "overall_status": "CRITICAL",
                        "alerts": [
                            {"level": "CRITICAL", "component": "PostgreSQL", "message": "Postgres database down"}
                        ],
                    },
                ),
                MagicMock(status_code=200, json=lambda: {"accounts": []}),
                MagicMock(status_code=200, json=lambda: {"accounts": []}),
                MagicMock(status_code=200, json=lambda: {"incidents": []}),
                MagicMock(status_code=200, json=lambda: {"accounts": []}),
            ])
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)

            res = await checker.check()
            assert res.passed is False
            assert res.gates["system_monitor"] == "UNSAFE"
            assert any("Postgres database down" in f for f in res.failures)

    asyncio.run(_run())


def test_kill_switch_active_blocks():
    """Test F: Kill switch active returns kill_switch = UNSAFE and passed = False."""
    settings = WatchdogSettings(backend_host="127.0.0.1", backend_port=8001)
    checker = SafetyGateChecker(settings)

    async def _run():
        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=[
                MagicMock(status_code=200, json=lambda: {"overall_status": "HEALTHY", "alerts": []}),
                MagicMock(
                    status_code=200,
                    json=lambda: {"accounts": [{"id": 1, "ibkr_account": "U123", "kill_switch_active": True}]},
                ),
                MagicMock(status_code=200, json=lambda: {"accounts": [{"ibkr_account": "U123"}]}),
                MagicMock(status_code=200, json=lambda: {"incidents": []}),
                MagicMock(status_code=200, json=lambda: {"accounts": []}),
            ])
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)

            res = await checker.check()
            assert res.passed is False
            assert res.gates["kill_switch"] == "UNSAFE"
            assert any("kill switch ACTIVE" in f for f in res.failures)

    asyncio.run(_run())


def test_basket_critical_blocks():
    """Test G: Basket critical incident returns baskets = UNSAFE and passed = False."""
    settings = WatchdogSettings(backend_host="127.0.0.1", backend_port=8001)
    checker = SafetyGateChecker(settings)

    async def _run():
        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=[
                MagicMock(status_code=200, json=lambda: {"overall_status": "HEALTHY", "alerts": []}),
                MagicMock(status_code=200, json=lambda: {"accounts": [{"id": 1, "ibkr_account": "U123", "kill_switch_active": False}]}),
                MagicMock(status_code=200, json=lambda: {"accounts": [{"ibkr_account": "U123"}]}),
                MagicMock(status_code=200, json=lambda: {"incidents": [{"basket_id": 99}]}),
                MagicMock(status_code=200, json=lambda: {"accounts": []}),
            ])
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)

            res = await checker.check()
            assert res.passed is False
            assert res.gates["baskets"] == "UNSAFE"
            assert any("BASKET_CRITICAL" in f for f in res.failures)

    asyncio.run(_run())


def test_trading_mode_unknown_port_blocks():
    """Test H: Unrecognized gateway port returns trading_mode = UNKNOWN and passed = False."""
    settings = WatchdogSettings(backend_host="127.0.0.1", backend_port=8001, gateway_port=9999)
    checker = SafetyGateChecker(settings)

    async def _run():
        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=[
                MagicMock(status_code=200, json=lambda: {"overall_status": "HEALTHY", "alerts": []}),
                MagicMock(status_code=200, json=lambda: {"accounts": [{"id": 1, "ibkr_account": "U123", "kill_switch_active": False}]}),
                MagicMock(status_code=200, json=lambda: {"accounts": [{"ibkr_account": "U123"}]}),
                MagicMock(status_code=200, json=lambda: {"incidents": []}),
                MagicMock(status_code=200, json=lambda: {"accounts": []}),
            ])
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)

            res = await checker.check()
            assert res.passed is False
            assert res.gates["trading_mode"] == "UNKNOWN"
            assert any("gateway port 9999 not recognized" in f for f in res.failures)

    asyncio.run(_run())
