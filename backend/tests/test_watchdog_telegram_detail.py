"""Telegram detail tests per spec — service-specific diagnostics, severity, ordering, safety."""

import asyncio

from app.services.watchdog.config import WatchdogSettings
from app.services.watchdog.health import (
    BackendHealthChecker,
    DemoHealthChecker,
    GatewayHealthChecker,
    PostgresHealthChecker,
    RedisHealthChecker,
    WebhookHealthChecker,
)
from app.services.watchdog.models import HealthResult, HealthStatus, NotificationEvent, ServiceName, ServiceSnapshot, ServiceState
from app.services.watchdog.notifier import format_telegram_message


def _snap(svc: ServiceName, state: ServiceState = ServiceState.FAILED) -> ServiceSnapshot:
    return ServiceSnapshot(service=svc, state=state, failure_reason="test")


# ---- ordering & severity ----

def test_format_ordering():
    snap = _snap(ServiceName.BACKEND)
    text = format_telegram_message(ServiceName.BACKEND, NotificationEvent.FAILURE, snap, host="h", port=8001)
    # order: WATCHDOG, EVENT, SERVICE, STATUS, WHAT HAPPENED, ERROR, WHERE, IMPACT, RECOVERY, ATTEMPT/TIME
    assert text.index("WATCHDOG") < text.index("EVENT:")
    assert text.index("EVENT:") < text.index("SERVICE:")
    assert text.index("SERVICE:") < text.index("STATUS:")
    assert text.index("STATUS:") < text.index("WHAT HAPPENED")
    assert text.index("WHAT HAPPENED") < text.index("ERROR / FAILURE DETAIL")
    assert text.index("ERROR / FAILURE DETAIL") < text.index("WHERE")
    assert text.index("WHERE") < text.index("IMPACT")
    assert text.index("IMPACT") < text.index("RECOVERY")
    assert text.index("RECOVERY") < text.index("TIME")


def test_severity_critical():
    snap = _snap(ServiceName.GATEWAY)
    text = format_telegram_message(ServiceName.GATEWAY, NotificationEvent.FAILURE, snap, host="h")
    assert "CRITICAL" in text
    assert "🚨" in text


def test_severity_warning():
    snap = _snap(ServiceName.BACKEND, ServiceState.DEGRADED)
    text = format_telegram_message(ServiceName.BACKEND, NotificationEvent.UNHEALTHY, snap, host="h")
    assert "WARNING" in text


def test_severity_info():
    snap = ServiceSnapshot(service=ServiceName.BACKEND, state=ServiceState.HEALTHY)
    text = format_telegram_message(ServiceName.BACKEND, NotificationEvent.RECOVERED, snap, host="h")
    assert "INFO" in text or "CRITICAL" not in text


# ---- secrets not leaked ----

def test_secrets_not_in_message():
    hr = HealthResult(service=ServiceName.BACKEND, status=HealthStatus.FAILED, detail="TELEGRAM_BOT_TOKEN=secret123")
    hr.underlying_error = "TELEGRAM_BOT_TOKEN=secret123"
    snap = ServiceSnapshot(service=ServiceName.BACKEND, state=ServiceState.FAILED, last_health=hr)
    text = format_telegram_message(ServiceName.BACKEND, NotificationEvent.FAILURE, snap, host="h", health=hr)
    assert "secret123" not in text
    assert "REDACTED" in text


def test_log_excerpt_bounded():
    hr = HealthResult(service=ServiceName.GATEWAY, status=HealthStatus.DEGRADED, detail="x", log_excerpt="a" * 1000)
    snap = _snap(ServiceName.GATEWAY, ServiceState.DEGRADED)
    snap.last_health = hr
    text = format_telegram_message(ServiceName.GATEWAY, NotificationEvent.UNHEALTHY, snap, host="h", health=hr)
    # log excerpt should be truncated to 400 in health, but formatter also bounds
    assert len(text) < 5000


def test_unknown_fields_do_not_crash():
    hr = HealthResult(service=ServiceName.BACKEND, status=HealthStatus.FAILED, detail="unknown")
    snap = _snap(ServiceName.BACKEND)
    snap.last_health = hr
    text = format_telegram_message(ServiceName.BACKEND, NotificationEvent.FAILURE, snap, host="h", health=hr)
    assert "Not available" in text or "unknown" in text.lower()


# ---- service-specific health diagnostics ----

def test_gateway_tcp_refused():
    s = WatchdogSettings(gateway_port=19997)
    checker = GatewayHealthChecker(s)

    async def _run():
        hr = await checker.check()
        assert hr.reason in ("tcp_refused", "xvfb_missing")
        assert "refused" in hr.detail.lower() or "Xvfb" in hr.detail
        assert hr.host == "127.0.0.1"
        assert hr.port == 19997

    asyncio.run(_run())


def test_gateway_login_marker():
    # when TCP open but marker missing, should be degraded with reason login_marker_missing
    # we can't easily open TCP without gateway, so test formatter path via manual HealthResult
    hr = HealthResult(service=ServiceName.GATEWAY, status=HealthStatus.DEGRADED, detail="TCP 127.0.0.1:4002 open (login marker not seen)", reason="login_marker_missing", host="127.0.0.1", port=4002, log_marker="Login has completed", log_excerpt="2026-08-31 14:21:03 ERROR ...", what_happened="IB Gateway process is running but IBKR login completion was not detected.")
    snap = ServiceSnapshot(service=ServiceName.GATEWAY, state=ServiceState.DEGRADED, last_health=hr)
    text = format_telegram_message(ServiceName.GATEWAY, NotificationEvent.UNHEALTHY, snap, host="h", health=hr)
    assert "login" in text.lower()
    assert "Login has completed" in text


def test_backend_tcp_refused():
    s = WatchdogSettings(backend_port=19998)
    checker = BackendHealthChecker(s)

    async def _run():
        hr = await checker.check()
        assert hr.reason == "tcp_refused"
        assert hr.endpoint_url and "19998" in hr.endpoint_url

    asyncio.run(_run())


def test_backend_readiness_degraded():
    hr = HealthResult(service=ServiceName.BACKEND, status=HealthStatus.DEGRADED, reason="readiness_degraded", host="127.0.0.1", port=8001, endpoint="/health/ready", underlying_error="TWS readiness check failed", what_happened="Trading Backend process is alive but is not ready.")
    snap = ServiceSnapshot(service=ServiceName.BACKEND, state=ServiceState.DEGRADED, last_health=hr)
    text = format_telegram_message(ServiceName.BACKEND, NotificationEvent.UNHEALTHY, snap, host="h", health=hr)
    assert "not ready" in text.lower()
    assert "TWS" in text


def test_webhook_postgres_dependency():
    hr = HealthResult(service=ServiceName.WEBHOOK, status=HealthStatus.DEGRADED, reason="readiness_failed_postgres", dependency="postgres", host="127.0.0.1", port=8000)
    snap = _snap(ServiceName.WEBHOOK, ServiceState.DEGRADED)
    text = format_telegram_message(ServiceName.WEBHOOK, NotificationEvent.UNHEALTHY, snap, host="h", health=hr)
    assert "PostgreSQL" in text or "postgres" in text.lower()


def test_demo_redis_degraded():
    hr = HealthResult(service=ServiceName.DEMO, status=HealthStatus.DEGRADED, reason="redis_degraded", dependency="redis", host="127.0.0.1", port=8010, underlying_error="Redis PING failed", what_happened="Demo Streaming health check failed — Redis PING failed.")
    snap = _snap(ServiceName.DEMO, ServiceState.DEGRADED)
    text = format_telegram_message(ServiceName.DEMO, NotificationEvent.UNHEALTHY, snap, host="h", health=hr)
    assert "Redis" in text
    assert "None" in text or "execution" in text.lower()  # trading impact none


def test_postgres_tcp_vs_sql():
    s = WatchdogSettings(postgres_port=19999, database_url="postgresql+asyncpg://root:root123@localhost:19999/ibkr_trading")
    checker = PostgresHealthChecker(s)

    async def _run():
        hr = await checker.check()
        assert hr.reason == "tcp_refused"
        assert "TCP" in hr.detail

    asyncio.run(_run())


def test_redis_tcp_vs_ping():
    s = WatchdogSettings(redis_port=19999)
    checker = RedisHealthChecker(s)

    async def _run():
        hr = await checker.check()
        assert hr.reason == "tcp_refused"

    asyncio.run(_run())


def test_recovery_messages():
    snap = ServiceSnapshot(service=ServiceName.BACKEND, state=ServiceState.RECOVERING, recovery_attempts=[])
    snap.last_health = HealthResult(service=ServiceName.BACKEND, status=HealthStatus.FAILED, reason="tcp_refused", underlying_error="ConnectionRefusedError")
    text = format_telegram_message(ServiceName.BACKEND, NotificationEvent.RECOVERY_STARTED, snap, host="h", attempt="2/5", health=snap.last_health)
    assert "RECOVERY_STARTED" in text or "RECOVERING" in text
    assert "2/5" in text
    assert "EXPECTED VERIFICATION" in text

    snap2 = ServiceSnapshot(service=ServiceName.BACKEND, state=ServiceState.RECOVERED)
    snap2.last_health = HealthResult(service=ServiceName.BACKEND, status=HealthStatus.HEALTHY, reason="healthy")
    text2 = format_telegram_message(ServiceName.BACKEND, NotificationEvent.RECOVERED, snap2, host="h", attempt="2/5", health=snap2.last_health, recovery_duration=23.4)
    assert "RECOVERED" in text2
    assert "23.4" in text2


def test_manual_intervention_message():
    snap = ServiceSnapshot(service=ServiceName.GATEWAY, state=ServiceState.MANUAL_INTERVENTION_REQUIRED)
    hr = HealthResult(service=ServiceName.GATEWAY, status=HealthStatus.FAILED, reason="login_marker_missing", underlying_error="Login not detected", what_happened="IB Gateway could not be recovered.")
    text = format_telegram_message(ServiceName.GATEWAY, NotificationEvent.MANUAL_INTERVENTION_REQUIRED, snap, host="h", attempt="5/5", health=hr)
    assert "MANUAL" in text
    assert "5/5" in text


def test_stop_vs_failure():
    snap_stop = ServiceSnapshot(service=ServiceName.DEMO, state=ServiceState.UNKNOWN)
    text_stop = format_telegram_message(ServiceName.DEMO, NotificationEvent.STOP, snap_stop, host="h")
    assert "STOP" in text_stop
    assert "intentionally" in text_stop.lower()

    snap_fail = ServiceSnapshot(service=ServiceName.DEMO, state=ServiceState.FAILED)
    text_fail = format_telegram_message(ServiceName.DEMO, NotificationEvent.FAILURE, snap_fail, host="h")
    assert "FAILURE" in text_fail
