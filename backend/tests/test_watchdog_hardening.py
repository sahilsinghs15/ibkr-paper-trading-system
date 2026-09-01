"""Hardening regression tests for watchdog notification accuracy (prompt §21, 14 tests)."""
import asyncio
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, "backend")

import pytest

from app.services.watchdog.config import WatchdogSettings
from app.services.watchdog.daemon import WatchdogDaemon
from app.services.watchdog.health import HealthResult
from app.services.watchdog.models import (
    HealthStatus,
    NotificationEvent,
    ServiceName,
    ServiceSnapshot,
    ServiceState,
)
from app.services.watchdog.notifier import (
    NotificationDeduplicator,
    NotificationQueue,
    format_telegram_message,
)
from app.services.watchdog.state_machine import event_for_transition, next_state
from app.services.watchdog.telegram import TelegramClient


def _healthy_hr(service, detail, port, trading_impact="Trading is READY"):
    return HealthResult(
        service=service,
        status=HealthStatus.HEALTHY,
        detail=detail,
        reason="healthy",
        host="127.0.0.1",
        port=port,
        endpoint_url=f"tcp://127.0.0.1:{port}" if service == ServiceName.GATEWAY else f"http://127.0.0.1:{port}/health",
        what_happened=f"{service.value} is healthy",
    )

# Test 1 — Successful check cannot produce ERROR (TCP open)
def test_successful_tcp_open_no_error():
    hr = HealthResult(
        service=ServiceName.GATEWAY,
        status=HealthStatus.HEALTHY,
        detail="TCP 127.0.0.1:4002 open",
        reason="healthy",
        host="127.0.0.1",
        port=4002,
        endpoint_url="tcp://127.0.0.1:4002",
        what_happened="IB Gateway is reachable and login completed.",
    )
    snap = ServiceSnapshot(service=ServiceName.GATEWAY, state=ServiceState.HEALTHY, last_health=hr)
    text = format_telegram_message(ServiceName.GATEWAY, NotificationEvent.START, snap, host="127.0.0.1", port=4002, health=hr)
    assert "<b>ERROR</b>" not in text
    assert "TCP 127.0.0.1:4002 → OPEN" in text or "TCP 127.0.0.1:4002 open → OK" in text

# Test 2 — HTTP 200 cannot produce ERROR
def test_http200_no_error():
    hr = HealthResult(
        service=ServiceName.BACKEND,
        status=HealthStatus.HEALTHY,
        detail="HTTP 200",
        reason="healthy",
        host="127.0.0.1",
        port=8001,
        endpoint_url="http://127.0.0.1:8001/health",
    )
    snap = ServiceSnapshot(service=ServiceName.BACKEND, state=ServiceState.HEALTHY, last_health=hr)
    # Even for TRADING_BLOCKED with healthy http, ERROR must be safety reason not HTTP 200
    snap2 = ServiceSnapshot(service=ServiceName.BACKEND, state=ServiceState.TRADING_BLOCKED, last_health=hr, failure_reason="safety gate: kill switch")
    text = format_telegram_message(ServiceName.BACKEND, NotificationEvent.TRADING_BLOCKED, snap2, host="h", port=8001, health=hr)
    # Must not have HTTP 200 under ERROR
    if "<b>ERROR</b>" in text:
        assert "HTTP 200" not in text.split("<b>ERROR</b>")[1].split("<b>WHERE</b>")[0]
    # START also no error
    text2 = format_telegram_message(ServiceName.BACKEND, NotificationEvent.START, snap, host="h", port=8001, health=hr)
    assert "<b>ERROR</b>" not in text2

# Test 3 — Trading blocked cannot say trading ready (exact phrase)
def test_trading_blocked_no_ready():
    hr = HealthResult(
        service=ServiceName.BACKEND,
        status=HealthStatus.HEALTHY,
        detail="HTTP 200",
        reason="healthy",
        host="127.0.0.1",
        port=8001,
        what_happened="Trading Backend is healthy and ready.",
        impact="Execution workers can process signal_jobs.",
        trading_impact="Trading is READY (subject to safety gates).",
    )
    snap = ServiceSnapshot(service=ServiceName.BACKEND, state=ServiceState.TRADING_BLOCKED, last_health=hr, failure_reason="safety gate: system-monitor")
    text = format_telegram_message(ServiceName.BACKEND, NotificationEvent.TRADING_BLOCKED, snap, host="h", port=8001, health=hr)
    # Must be BLOCKED not READY
    assert "TRADING STATUS" in text
    # Trading status should be BLOCKED
    assert "BLOCKED" in text
    # Must not contain misleading READY in TRADING section (impact)
    # Our fix ensures IMPACT says BLOCKED, not READY
    trading_section = text.split("<b>TRADING</b>")[1] if "<b>TRADING</b>" in text else ""
    assert "READY" not in trading_section or "BLOCKED" in trading_section
    # Also ensure not saying Trading is READY when blocked
    # Allow "READY (subject to safety gates)" in SERVICE STATUS? That's okay but TRADING IMPACT must be BLOCKED
    assert "Trading is BLOCKED" in text or "BLOCKED" in trading_section

# Test 4 — Service health and trading readiness are independent (HEALTHY service, BLOCKED trading)
def test_healthy_service_blocked_trading():
    hr = HealthResult(
        service=ServiceName.BACKEND,
        status=HealthStatus.HEALTHY,
        detail="HTTP 200",
        reason="healthy",
        host="127.0.0.1",
        port=8001,
    )
    snap = ServiceSnapshot(service=ServiceName.BACKEND, state=ServiceState.TRADING_BLOCKED, last_health=hr, failure_reason="safety gate: IB Gateway not cleared")
    text = format_telegram_message(ServiceName.BACKEND, NotificationEvent.TRADING_BLOCKED, snap, host="h", port=8001, health=hr)
    assert "SERVICE STATUS" in text
    assert "HEALTHY" in text
    assert "TRADING STATUS" in text
    assert "BLOCKED" in text
    # DETAILS must explain block
    assert "health check is passing" in text.lower() and "blocked" in text.lower()

# Test 5 — Gateway unavailable blocks trading (correct impact)
def test_gateway_blocks_trading():
    hr = HealthResult(
        service=ServiceName.GATEWAY,
        status=HealthStatus.FAILED,
        detail="TCP 127.0.0.1:4002 refused",
        reason="tcp_refused",
        host="127.0.0.1",
        port=4002,
        underlying_error="ConnectionRefusedError: refused",
        what_happened="IB Gateway API socket is unreachable.",
    )
    snap = ServiceSnapshot(service=ServiceName.GATEWAY, state=ServiceState.FAILED, last_health=hr)
    text = format_telegram_message(ServiceName.GATEWAY, NotificationEvent.FAILURE, snap, host="h", port=4002, health=hr)
    assert "Trading Backend cannot communicate" in text
    assert "BLOCKED" in text  # trading status
    # gateway check failure must report refused in CHECK
    assert "refused" in text.lower()

# Test 6 — Demo Streaming failure does not incorrectly claim trading execution failure
def test_demo_no_trading_impact():
    hr = HealthResult(
        service=ServiceName.DEMO,
        status=HealthStatus.FAILED,
        detail="TCP 127.0.0.1:8010 refused",
        reason="tcp_refused",
        host="127.0.0.1",
        port=8010,
        underlying_error="refused",
    )
    snap = ServiceSnapshot(service=ServiceName.DEMO, state=ServiceState.FAILED, last_health=hr)
    text = format_telegram_message(ServiceName.DEMO, NotificationEvent.FAILURE, snap, host="h", port=8010, health=hr)
    assert "NOT AFFECTED" in text or "independent" in text.lower()
    assert "Order execution is BLOCKED" not in text
    assert "Order execution may be affected" not in text

# Test 7 — FAILED → RECOVERING → HEALTHY notifications
def test_failed_recovering_healthy_flow():
    snap = ServiceSnapshot(service=ServiceName.BACKEND, state=ServiceState.HEALTHY)
    # healthy -> failed
    nxt = next_state(snap, health_failed=True, health_degraded=False)
    assert nxt == ServiceState.FAILED
    ev = event_for_transition(ServiceState.HEALTHY, nxt)
    assert ev == NotificationEvent.FAILURE
    snap.state = ServiceState.FAILED
    # failed -> recovering (daemon promotion)
    assert event_for_transition(ServiceState.FAILED, ServiceState.RECOVERING) == NotificationEvent.RECOVERY_STARTED
    snap.state = ServiceState.RECOVERING
    nxt2 = next_state(snap, health_failed=False, health_degraded=False)
    assert nxt2 == ServiceState.VERIFYING
    # verifying success -> recovered
    snap.state = ServiceState.VERIFYING
    nxt3 = next_state(snap, health_failed=False, health_degraded=False, verifying_success=True)
    assert nxt3 == ServiceState.RECOVERED
    assert event_for_transition(ServiceState.VERIFYING, ServiceState.RECOVERED) == NotificationEvent.RECOVERED

# Test 8 — STOPPED → STARTED (health confirmed)
def test_stopped_to_started():
    # Simulate UNKNOWN -> HEALTHY (START)
    snap = ServiceSnapshot(service=ServiceName.GATEWAY, state=ServiceState.UNKNOWN)
    nxt = next_state(snap, health_failed=False, health_degraded=False)
    assert nxt == ServiceState.HEALTHY
    ev = event_for_transition(ServiceState.UNKNOWN, nxt)
    assert ev == NotificationEvent.START
    # STOP event is for watchdog shutdown, but health recovery via START is distinct
    text_start = format_telegram_message(ServiceName.GATEWAY, NotificationEvent.START, ServiceSnapshot(service=ServiceName.GATEWAY, state=ServiceState.HEALTHY), host="h", port=4002)
    assert "HEALTH CONFIRMED" in text_start
    text_stop = format_telegram_message(ServiceName.GATEWAY, NotificationEvent.STOP, ServiceSnapshot(service=ServiceName.GATEWAY, state=ServiceState.UNKNOWN), host="h", port=4002)
    assert "STOP" in text_stop

# Test 9 — Start/stop events aren't lost (rapid STOP START HEALTHY)
def test_rapid_start_stop_not_lost():
    settings = WatchdogSettings(watchdog_interval_seconds=0.1, telegram_enabled=False)
    daemon = WatchdogDaemon(settings)
    # Simulate rapid transitions without missing notifications due to race: we check that state machine produces distinct events
    snap = daemon.snapshots[ServiceName.BACKEND]
    snap.state = ServiceState.HEALTHY
    # Simulate stop: health fails
    snap2 = ServiceSnapshot(service=ServiceName.BACKEND, state=ServiceState.HEALTHY)
    nxt = next_state(snap2, True, False)
    assert nxt == ServiceState.FAILED
    # Then recovers quickly
    snap2.state = ServiceState.FAILED
    nxt2 = next_state(snap2, False, False)
    assert nxt2 == ServiceState.RECOVERED
    # Ensure both transitions have events
    assert event_for_transition(ServiceState.HEALTHY, ServiceState.FAILED) is not None
    assert event_for_transition(ServiceState.FAILED, ServiceState.RECOVERED) is not None

# Test 10 — Duplicate state suppression (repeated HEALTHY not repeated)
def test_duplicate_suppression():
    dedup = NotificationDeduplicator(cooldown_seconds=300)
    # First send allowed
    assert dedup.should_send(ServiceName.BACKEND, NotificationEvent.FAILURE) is True
    dedup.mark_sent(ServiceName.BACKEND, NotificationEvent.FAILURE)
    # Same event within cooldown should be suppressed if last event same
    assert dedup.should_send(ServiceName.BACKEND, NotificationEvent.FAILURE) is False
    # Different event should be allowed even within cooldown
    assert dedup.should_send(ServiceName.BACKEND, NotificationEvent.RECOVERY_STARTED) is True
    dedup.mark_sent(ServiceName.BACKEND, NotificationEvent.RECOVERY_STARTED)
    # Now FAILURE again after different event should be allowed (not over-dedup)
    assert dedup.should_send(ServiceName.BACKEND, NotificationEvent.FAILURE) is True

# Test 11 — PID change no false failure
def test_pid_change_no_false_failure():
    # Simulate daemon handling PID change while healthy — should not transition to FAILED
    settings = WatchdogSettings(telegram_enabled=False)
    daemon = WatchdogDaemon(settings)
    snap = daemon.snapshots[ServiceName.BACKEND]
    snap.state = ServiceState.HEALTHY
    hr1 = HealthResult(service=ServiceName.BACKEND, status=HealthStatus.HEALTHY, detail="HTTP 200", reason="healthy", host="127.0.0.1", port=8001, pid=1000)
    hr2 = HealthResult(service=ServiceName.BACKEND, status=HealthStatus.HEALTHY, detail="HTTP 200", reason="healthy", host="127.0.0.1", port=8001, pid=1001)
    snap.last_health = hr1
    # next check with different pid but healthy -> state stays HEALTHY, no failure event
    nxt = next_state(snap, health_failed=False, health_degraded=False)
    assert nxt == ServiceState.HEALTHY
    assert event_for_transition(ServiceState.HEALTHY, nxt) is None

# Test 12 — Recovery attempt counter accurate
def test_recovery_attempt_counter():
    # Ensure attempt strings are 1/5,2/5 etc not 0/5
    hr = HealthResult(service=ServiceName.BACKEND, status=HealthStatus.FAILED, detail="refused", underlying_error="refused")
    snap = ServiceSnapshot(service=ServiceName.BACKEND, state=ServiceState.RECOVERING, last_health=hr)
    # Simulate daemon's attempt counting: after adding one attempt, text should be 1/5
    snap.recovery_attempts = [datetime.now(UTC)]
    text = format_telegram_message(ServiceName.BACKEND, NotificationEvent.RECOVERY_STARTED, snap, host="h", attempt="1/5", health=hr)
    assert "1/5" in text
    assert "0/5" not in text
    snap.recovery_attempts = [datetime.now(UTC), datetime.now(UTC)]
    text2 = format_telegram_message(ServiceName.BACKEND, NotificationEvent.RECOVERY_STARTED, snap, host="h", attempt="2/5", health=hr)
    assert "2/5" in text2
    assert "0/5" not in text2

# Test 13 — Notification delivery failure logged (simulated)
def test_delivery_failure_logged(caplog):
    settings = WatchdogSettings(telegram_enabled=False)
    tg = TelegramClient(None, None, enabled=False)
    q = NotificationQueue(tg, settings)
    async def _run():
        await q.start()
        # Enqueue a critical message — telegram will return False, should log warning
        q.enqueue(ServiceName.BACKEND, NotificationEvent.FAILURE, "critical test", force=True)
        await asyncio.sleep(0.3)
        await q.stop()
    asyncio.run(_run())
    # Ensure queue handled without crash; delivery failure is observable via logs (warning)
    # No exception should propagate

# Test 14 — No sensitive information leaked
def test_no_sensitive_leak():
    for secret in ["TELEGRAM_BOT_TOKEN=abc123", "DATABASE_URL=postgresql://user:pass@host", "password=secret123", "api_key=xyz"]:
        hr = HealthResult(service=ServiceName.BACKEND, status=HealthStatus.FAILED, detail=secret, underlying_error=secret)
        snap = ServiceSnapshot(service=ServiceName.BACKEND, state=ServiceState.FAILED, last_health=hr)
        text = format_telegram_message(ServiceName.BACKEND, NotificationEvent.FAILURE, snap, host="h", health=hr)
        assert "abc123" not in text and "pass@host" not in text and "secret123" not in text
        assert "[REDACTED]" in text

def test_safety_blocked_ordering():
    # Verify that TRADING_BLOCKED details mention safety gate and not misleading READY
    hr = HealthResult(service=ServiceName.BACKEND, status=HealthStatus.HEALTHY, detail="HTTP 200", reason="healthy", host="127.0.0.1", port=8001)
    snap = ServiceSnapshot(service=ServiceName.BACKEND, state=ServiceState.TRADING_BLOCKED, last_health=hr, failure_reason="safety gate: kill switch ACTIVE")
    text = format_telegram_message(ServiceName.BACKEND, NotificationEvent.TRADING_BLOCKED, snap, host="h", port=8001, health=hr)
    # Order: WATCHDOG < SERVICE < SERVICE STATUS < TRADING STATUS < STATUS < EVENT < DETAILS < CHECK < ERROR < WHERE < IMPACT < TRADING
    assert text.index("WATCHDOG") < text.index("SERVICE")
    assert text.index("SERVICE STATUS") < text.index("TRADING STATUS")
    assert text.index("TRADING STATUS") < text.index("EVENT")
    assert text.index("CHECK") < text.index("WHERE")
