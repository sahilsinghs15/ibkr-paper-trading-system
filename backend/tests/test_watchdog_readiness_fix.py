"""Unit tests for Watchdog readiness check hardening and auth fallback fix."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.watchdog.config import WatchdogSettings
from app.services.watchdog.daemon import WatchdogDaemon
from app.services.watchdog.health import BackendHealthChecker
from app.services.watchdog.models import (
    HealthResult,
    HealthStatus,
    ServiceName,
    ServiceSnapshot,
    ServiceState,
)
from app.services.watchdog.notifier import NotificationEvent, format_telegram_message
from app.services.watchdog.safety import SafetyGateChecker


@pytest.fixture
def watchdog_settings() -> WatchdogSettings:
    return WatchdogSettings(
        backend_host="127.0.0.1",
        backend_port=8001,
        market_closed_enabled=False,
    )


@pytest.mark.asyncio
async def test_backend_health_checker_healthy(watchdog_settings: WatchdogSettings):
    """Test 1: Healthy /health (200) and /health/ready (200) returns HealthStatus.HEALTHY."""
    checker = BackendHealthChecker(watchdog_settings)

    async def mock_http_get(url: str, timeout: float = 3.5, headers: dict | None = None):
        if url.endswith(("/health", "/health/ready")):
            return True, "HTTP 200", 1.5
        return False, "HTTP 404", None

    with patch("app.services.watchdog.health._http_get", side_effect=mock_http_get):
        result = await checker.check()
        assert result.status == HealthStatus.HEALTHY
        assert result.liveness == HealthStatus.HEALTHY
        assert result.readiness == HealthStatus.HEALTHY
        assert result.reason == "healthy"


@pytest.mark.asyncio
async def test_backend_health_checker_authenticated_fallback(watchdog_settings: WatchdogSettings):
    """Test 2: /health/ready times out, but fallback /api/v1/system-monitor with auth headers succeeds."""
    checker = BackendHealthChecker(watchdog_settings)
    captured_headers: dict = {}

    async def mock_http_get(url: str, timeout: float = 3.5, headers: dict | None = None):
        if url.endswith("/health"):
            return True, "HTTP 200", 1.5
        if url.endswith("/health/ready"):
            return False, "ReadTimeout: ", None
        if url.endswith("/api/v1/system-monitor"):
            if headers and "Authorization" in headers and headers["Authorization"].startswith("Bearer "):
                captured_headers.update(headers)
                return True, "HTTP 200", 2.0
            return False, "HTTP 401", None
        return False, "HTTP 404", None

    with patch("app.services.watchdog.health._http_get", side_effect=mock_http_get):
        result = await checker.check()
        assert result.status == HealthStatus.HEALTHY
        assert result.reason == "healthy"
        assert "Authorization" in captured_headers
        assert captured_headers["Authorization"].startswith("Bearer ")


@pytest.mark.asyncio
async def test_backend_transient_readiness_debounce(watchdog_settings: WatchdogSettings):
    """Test 3: Single readiness timeout does not transition healthy backend to DEGRADED."""
    daemon = WatchdogDaemon(watchdog_settings)
    daemon.snapshots[ServiceName.BACKEND] = ServiceSnapshot(
        service=ServiceName.BACKEND,
        state=ServiceState.HEALTHY,
        consecutive_failures=0,
        consecutive_successes=10,
    )

    # First poll: readiness unconfirmed (degraded result)
    degraded_hr = HealthResult(
        service=ServiceName.BACKEND,
        status=HealthStatus.DEGRADED,
        liveness=HealthStatus.HEALTHY,
        readiness=HealthStatus.DEGRADED,
        reason="readiness_unconfirmed",
        underlying_error="ReadTimeout",
    )

    with patch.object(daemon.checkers[ServiceName.BACKEND], "check", AsyncMock(return_value=degraded_hr)):
        await daemon._check_one(ServiceName.BACKEND)
        # Should stay HEALTHY due to single transient failure (debounce)
        assert daemon.snapshots[ServiceName.BACKEND].state == ServiceState.HEALTHY

    # Second poll: readiness still degraded -> now transitions to DEGRADED
    with patch.object(daemon.checkers[ServiceName.BACKEND], "check", AsyncMock(return_value=degraded_hr)):
        await daemon._check_one(ServiceName.BACKEND)
        assert daemon.snapshots[ServiceName.BACKEND].state == ServiceState.DEGRADED


@pytest.mark.asyncio
async def test_backend_genuine_readiness_degradation(watchdog_settings: WatchdogSettings):
    """Test 4: Genuine readiness degradation (e.g. status=degraded, reason=tws_disconnected) reports readiness_degraded."""
    checker = BackendHealthChecker(watchdog_settings)

    class MockResp:
        status_code = 200
        def json(self):
            return {"status": "degraded", "reason": "tws_disconnected"}

    class MockClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, url):
            return MockResp()

    async def mock_http_get(url: str, timeout: float = 3.5, headers: dict | None = None):
        return True, "HTTP 200", 1.5

    with patch("app.services.watchdog.health._http_get", side_effect=mock_http_get), \
         patch("httpx.AsyncClient", return_value=MockClient()):
        result = await checker.check()
        assert result.status == HealthStatus.DEGRADED
        assert result.reason == "readiness_degraded"
        assert result.underlying_error == "tws_disconnected"


@pytest.mark.asyncio
async def test_safety_gates_fail_closed(watchdog_settings: WatchdogSettings):
    """Test 5: Verify kill switch and safety gates remain fail-closed and functional."""
    checker = SafetyGateChecker(watchdog_settings)

    class MockResp:
        def __init__(self, url: str):
            self.url = url
            self.status_code = 200

        def json(self):
            if "system-monitor" in self.url:
                return {"overall_status": "HEALTHY", "alerts": []}
            if "critical" in self.url:
                return {"incidents": []}
            if "accounts" in self.url:
                return {"accounts": [{"ibkr_account": "DU123456", "port": 7497, "kill_switch_active": True}]}
            return {}

    class MockClient:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, url: str, **kwargs):
            return MockResp(url)

    with patch("httpx.AsyncClient", MockClient):
        res = await checker.check()
        assert res.passed is False
        assert res.gates["kill_switch"] == "UNSAFE"


def test_telegram_message_formatting_readiness_unconfirmed():
    """Verify Telegram message formatting for readiness_unconfirmed reports MONITORING UNCONFIRMED."""
    snap = ServiceSnapshot(service=ServiceName.BACKEND, state=ServiceState.HEALTHY)
    health = HealthResult(
        service=ServiceName.BACKEND,
        status=HealthStatus.DEGRADED,
        liveness=HealthStatus.HEALTHY,
        readiness=HealthStatus.DEGRADED,
        reason="readiness_unconfirmed",
        detail="HTTP 200 | readiness unconfirmed: ReadTimeout",
    )
    msg = format_telegram_message(
        service=ServiceName.BACKEND,
        event=NotificationEvent.UNHEALTHY,
        snapshot=snap,
        host="127.0.0.1",
        port=8001,
        health=health,
    )
    assert "MONITORING UNCONFIRMED (execution active)" in msg
