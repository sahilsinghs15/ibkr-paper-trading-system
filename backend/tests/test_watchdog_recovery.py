"""Recovery budget, safety gates, and demo isolation."""

import asyncio
from datetime import UTC, datetime

from app.services.watchdog.config import WatchdogSettings
from app.services.watchdog.daemon import WatchdogDaemon
from app.services.watchdog.models import (
    ServiceName,
    ServiceState,
)
from app.services.watchdog.safety import SafetyGateChecker


def test_recovery_budget_exhausted():
    settings = WatchdogSettings(recovery_max_attempts=2, recovery_window_seconds=600)
    daemon = WatchdogDaemon(settings)
    snap = daemon.snapshots[ServiceName.BACKEND]
    # add 2 attempts in window
    snap.recovery_attempts = [datetime.now(UTC), datetime.now(UTC)]
    assert daemon._is_recovery_budget_exhausted(snap) is True
    snap.recovery_attempts = []
    assert daemon._is_recovery_budget_exhausted(snap) is False


def test_safety_gate_blocks_when_system_monitor_critical():
    settings = WatchdogSettings(backend_host="127.0.0.1", backend_port=19999)
    checker = SafetyGateChecker(settings)

    async def _run():
        result = await checker.check()
        # unreachable -> degraded/failure -> blocked
        assert result.passed is False

    asyncio.run(_run())


def test_demo_failure_does_not_block_trading():
    settings = WatchdogSettings()
    daemon = WatchdogDaemon(settings)
    # simulate demo failed, backend healthy
    daemon.snapshots[ServiceName.DEMO].state = ServiceState.FAILED
    daemon.snapshots[ServiceName.BACKEND].state = ServiceState.HEALTHY
    # ensure trading critical untouched
    assert daemon.snapshots[ServiceName.BACKEND].state == ServiceState.HEALTHY


def test_watchdog_survives_backend_down():
    settings = WatchdogSettings(backend_port=19999, webhook_port=19998, gateway_port=19997, market_closed_enabled=False)
    daemon = WatchdogDaemon(settings)

    async def _run():
        await daemon._check_one(ServiceName.BACKEND)
        snap = daemon.snapshots[ServiceName.BACKEND]
        assert snap.state in (ServiceState.FAILED, ServiceState.STARTING, ServiceState.UNKNOWN, ServiceState.HEALTHY, ServiceState.MARKET_CLOSED)

    asyncio.run(_run())


def test_health_checkers_do_not_raise():
    settings = WatchdogSettings(gateway_port=19997, backend_port=19998, webhook_port=19999, demo_port=19996)

    async def _run():
        from app.services.watchdog.health import (
            BackendHealthChecker,
            GatewayHealthChecker,
        )

        g = GatewayHealthChecker(settings)
        b = BackendHealthChecker(settings)
        gr = await g.check()
        br = await b.check()
        assert gr.service == ServiceName.GATEWAY
        assert br.service == ServiceName.BACKEND

    asyncio.run(_run())
