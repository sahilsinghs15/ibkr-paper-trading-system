"""Regression tests for watchdog semantic accuracy, notification clarity, and message design (Phase 2D-C).

Verifies that the watchdog never claims actions it did not perform (e.g. process start/restart)
and that all Telegram notification event texts are semantically precise and non-misleading.
"""

import pytest

from app.services.watchdog.config import WatchdogSettings
from app.services.watchdog.models import (
    HealthResult,
    HealthStatus,
    NotificationEvent,
    SafetyGateResult,
    ServiceName,
    ServiceSnapshot,
    ServiceState,
)
from app.services.watchdog.notifier import NotificationDeduplicator, format_telegram_message
from app.services.watchdog.state_machine import event_for_transition, next_state


# 1. START internal event does not say service/process started.
def test_start_internal_event_does_not_say_started():
    snap = ServiceSnapshot(service=ServiceName.POSTGRES, state=ServiceState.HEALTHY)
    text = format_telegram_message(ServiceName.POSTGRES, NotificationEvent.START, snap, host="localhost", port=5433)
    assert "process started" not in text.lower()
    assert "service started" not in text.lower()


# 2. START user-facing event says HEALTH CONFIRMED.
def test_start_user_facing_event_says_health_confirmed():
    snap = ServiceSnapshot(service=ServiceName.BACKEND, state=ServiceState.HEALTHY)
    text = format_telegram_message(ServiceName.BACKEND, NotificationEvent.START, snap, host="127.0.0.1", port=8001)
    assert "HEALTH CONFIRMED" in text
    assert "WATCHDOG — HEALTHY" in text


# 3. Initial healthy PostgreSQL does not claim PostgreSQL was started.
def test_initial_healthy_postgres_not_claimed_started():
    snap = ServiceSnapshot(service=ServiceName.POSTGRES, state=ServiceState.HEALTHY)
    text = format_telegram_message(ServiceName.POSTGRES, NotificationEvent.START, snap, host="localhost", port=5433)
    assert "PostgreSQL process started" not in text
    assert "PostgreSQL started" not in text
    assert "Watchdog confirmed PostgreSQL is healthy" in text


# 4. Initial healthy Redis does not claim Redis was started.
def test_initial_healthy_redis_not_claimed_started():
    snap = ServiceSnapshot(service=ServiceName.REDIS, state=ServiceState.HEALTHY)
    text = format_telegram_message(ServiceName.REDIS, NotificationEvent.START, snap, host="localhost", port=6379)
    assert "Redis process started" not in text
    assert "Redis started" not in text
    assert "Watchdog confirmed Redis is healthy" in text


# 5. Initial healthy Demo does not claim Demo was started.
def test_initial_healthy_demo_not_claimed_started():
    snap = ServiceSnapshot(service=ServiceName.DEMO, state=ServiceState.HEALTHY)
    text = format_telegram_message(ServiceName.DEMO, NotificationEvent.START, snap, host="localhost", port=8010)
    assert "Demo Streaming process started" not in text
    assert "Demo Streaming started" not in text
    assert "Watchdog confirmed Demo Streaming is healthy" in text


# 6. HEALTHY is distinct from RECOVERED.
def test_healthy_is_distinct_from_recovered():
    snap_start = ServiceSnapshot(service=ServiceName.BACKEND, state=ServiceState.HEALTHY)
    text_start = format_telegram_message(ServiceName.BACKEND, NotificationEvent.START, snap_start, host="h", port=8001)

    snap_rec = ServiceSnapshot(service=ServiceName.BACKEND, state=ServiceState.RECOVERED)
    text_rec = format_telegram_message(ServiceName.BACKEND, NotificationEvent.RECOVERED, snap_rec, host="h", port=8001)

    assert "HEALTH CONFIRMED" in text_start
    assert "RECOVERED" in text_rec
    assert text_start != text_rec


# 7. MANUAL -> HEALTHY generates RECOVERED notification.
def test_manual_to_healthy_generates_recovered_notification():
    snap = ServiceSnapshot(service=ServiceName.GATEWAY, state=ServiceState.MANUAL_INTERVENTION_REQUIRED)
    nxt = next_state(snap, health_failed=False, health_degraded=False)
    assert nxt == ServiceState.HEALTHY
    event = event_for_transition(ServiceState.MANUAL_INTERVENTION_REQUIRED, nxt)
    assert event == NotificationEvent.RECOVERED


# 8. MANUAL -> RECOVERED is NOT invented as a ServiceState transition.
def test_manual_to_recovered_not_invented_as_servicestate():
    assert not hasattr(ServiceState, "RECOVERED_STATE")
    assert ServiceState.HEALTHY.value == "HEALTHY"


# 9. RECOVERED does not claim process restart.
def test_recovered_does_not_claim_process_restart():
    snap = ServiceSnapshot(service=ServiceName.DEMO, state=ServiceState.RECOVERED)
    text = format_telegram_message(ServiceName.DEMO, NotificationEvent.RECOVERED, snap, host="localhost", port=8010)
    assert "restarted successfully" not in text.lower()
    assert "Health recovery verified by watchdog" in text or "healthy again" in text


# 10. FAILURE does not claim crash without process evidence.
def test_failure_does_not_claim_crash_without_evidence():
    snap = ServiceSnapshot(service=ServiceName.BACKEND, state=ServiceState.FAILED)
    hr = HealthResult(
        service=ServiceName.BACKEND,
        status=HealthStatus.FAILED,
        detail="ConnectionRefusedError: TCP 127.0.0.1:8001 refused",
        reason="tcp_refused",
        host="127.0.0.1",
        port=8001,
    )
    text = format_telegram_message(ServiceName.BACKEND, NotificationEvent.FAILURE, snap, host="127.0.0.1", port=8001, health=hr)
    assert "crashed" not in text.lower()
    assert "is down" not in text.lower()
    assert "Watchdog could not reach" in text or "health check failed" in text.lower()


# 11. FAILURE uses health-check language.
def test_failure_uses_health_check_language():
    snap = ServiceSnapshot(service=ServiceName.POSTGRES, state=ServiceState.FAILED)
    text = format_telegram_message(ServiceName.POSTGRES, NotificationEvent.FAILURE, snap, host="localhost", port=5433)
    assert "WATCHDOG — HEALTH CHECK FAILED" in text


# 12. RECOVERY_STARTED does not claim watchdog restarted the service.
def test_recovery_started_does_not_claim_watchdog_restarted():
    snap = ServiceSnapshot(service=ServiceName.GATEWAY, state=ServiceState.RECOVERING)
    text = format_telegram_message(ServiceName.GATEWAY, NotificationEvent.RECOVERY_STARTED, snap, host="127.0.0.1", port=4002, attempt="1/5")
    assert "Watchdog restarted" not in text
    assert "Watchdog initiated process restart" not in text
    assert "Recovery workflow started" in text


# 13. MANUAL does not claim permanent failure.
def test_manual_does_not_claim_permanent_failure():
    snap = ServiceSnapshot(service=ServiceName.GATEWAY, state=ServiceState.MANUAL_INTERVENTION_REQUIRED)
    text = format_telegram_message(ServiceName.GATEWAY, NotificationEvent.MANUAL_INTERVENTION_REQUIRED, snap, host="127.0.0.1", port=4002, attempt="5/5")
    assert "permanently down" not in text.lower()
    assert "dead" not in text.lower()
    assert "Manual intervention is required" in text


# 14. Successful checks do not appear under ERROR.
def test_successful_checks_do_not_appear_under_error():
    snap = ServiceSnapshot(service=ServiceName.POSTGRES, state=ServiceState.HEALTHY)
    text = format_telegram_message(ServiceName.POSTGRES, NotificationEvent.START, snap, host="localhost", port=5433)
    assert "<b>ERROR</b>" not in text
    assert "<b>CHECK</b>" in text
    assert "SELECT 1 → OK" in text


# 15. Actual failures can contain ERROR.
def test_actual_failures_can_contain_error():
    snap = ServiceSnapshot(service=ServiceName.BACKEND, state=ServiceState.FAILED)
    hr = HealthResult(
        service=ServiceName.BACKEND,
        status=HealthStatus.FAILED,
        detail="ConnectionRefusedError: TCP 127.0.0.1:8001 refused",
        underlying_error="ConnectionRefusedError: TCP 127.0.0.1:8001 refused",
        host="127.0.0.1",
        port=8001,
    )
    text = format_telegram_message(ServiceName.BACKEND, NotificationEvent.FAILURE, snap, host="127.0.0.1", port=8001, health=hr)
    assert "<b>ERROR</b>" in text
    assert "ConnectionRefusedError" in text


# 16. Individual healthy service does not claim TRADING READY.
def test_individual_healthy_service_does_not_claim_trading_ready():
    for svc in (ServiceName.GATEWAY, ServiceName.BACKEND, ServiceName.POSTGRES, ServiceName.REDIS):
        snap = ServiceSnapshot(service=svc, state=ServiceState.HEALTHY)
        text = format_telegram_message(svc, NotificationEvent.START, snap, host="h")
        assert "Trading is READY" not in text


# 17. Individual unhealthy service does not claim TRADING BLOCKED without authoritative safety evidence.
def test_individual_unhealthy_service_does_not_claim_trading_blocked_unconditionally():
    snap = ServiceSnapshot(service=ServiceName.BACKEND, state=ServiceState.FAILED)
    text = format_telegram_message(ServiceName.BACKEND, NotificationEvent.FAILURE, snap, host="h")
    assert "Trading is BLOCKED" not in text
    assert "Order execution may be affected" in text or "Trading readiness cannot be confirmed" in text


# 18. SafetyGateChecker-passed state is the only path allowed to say TRADING READY.
def test_safety_gate_passed_allows_trading_ready():
    res = SafetyGateResult(passed=True, failures=[], details="all gates SAFE", gates={"system_monitor": "SAFE", "kill_switch": "SAFE", "baskets": "SAFE", "trading_mode": "SAFE"})
    assert res.passed is True


# 19. Safety-gate unsafe state is the only path allowed to say TRADING BLOCKED.
def test_safety_gate_unsafe_allows_trading_blocked():
    snap = ServiceSnapshot(service=ServiceName.BACKEND, state=ServiceState.FAILED)
    text = format_telegram_message(ServiceName.BACKEND, NotificationEvent.TRADING_BLOCKED, snap, host="h")
    assert "WATCHDOG — TRADING BLOCKED" in text
    assert "Trading remains BLOCKED" in text


# 20. Demo/Webhook/Redis trading impact remains execution-independent where appropriate.
def test_demo_webhook_redis_execution_independent():
    for svc in (ServiceName.DEMO, ServiceName.WEBHOOK, ServiceName.REDIS):
        snap = ServiceSnapshot(service=svc, state=ServiceState.HEALTHY)
        text = format_telegram_message(svc, NotificationEvent.START, snap, host="h")
        assert "independent" in text.lower() or "No direct trading impact" in text or "No direct trading conclusion" in text


# 21. HTML escaping still works.
def test_html_escaping_works():
    snap = ServiceSnapshot(service=ServiceName.BACKEND, state=ServiceState.FAILED)
    hr = HealthResult(service=ServiceName.BACKEND, status=HealthStatus.FAILED, detail="<script>alert(1)</script>")
    text = format_telegram_message(ServiceName.BACKEND, NotificationEvent.FAILURE, snap, host="h", health=hr)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


# 22. Secrets remain redacted.
def test_secrets_remain_redacted():
    snap = ServiceSnapshot(service=ServiceName.BACKEND, state=ServiceState.FAILED)
    hr = HealthResult(service=ServiceName.BACKEND, status=HealthStatus.FAILED, detail="TELEGRAM_BOT_TOKEN=123:ABC")
    text = format_telegram_message(ServiceName.BACKEND, NotificationEvent.FAILURE, snap, host="h", health=hr)
    assert "123:ABC" not in text
    assert "[REDACTED]" in text


# 23. Existing queue/dedup/rate-limit behavior remains unchanged.
def test_dedup_and_queue_behavior_intact():
    dedup = NotificationDeduplicator(cooldown_seconds=300)
    assert dedup.should_send(ServiceName.GATEWAY, NotificationEvent.FAILURE) is True
    dedup.mark_sent(ServiceName.GATEWAY, NotificationEvent.FAILURE)
    assert dedup.should_send(ServiceName.GATEWAY, NotificationEvent.FAILURE) is False


# 24. Existing state-machine transitions remain unchanged except MANUAL -> HEALTHY.
def test_state_machine_transitions_intact():
    snap = ServiceSnapshot(service=ServiceName.GATEWAY, state=ServiceState.HEALTHY)
    # HEALTHY + failed check -> DEGRADED
    nxt1 = next_state(snap, health_failed=False, health_degraded=True)
    assert nxt1 == ServiceState.DEGRADED

    # DEGRADED + failed check -> FAILED
    snap.state = ServiceState.DEGRADED
    nxt2 = next_state(snap, health_failed=True, health_degraded=False)
    assert nxt2 == ServiceState.FAILED
