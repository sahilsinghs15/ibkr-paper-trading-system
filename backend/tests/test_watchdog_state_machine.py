"""Watchdog state machine tests."""

from app.services.watchdog.models import NotificationEvent, ServiceName, ServiceSnapshot, ServiceState
from app.services.watchdog.state_machine import event_for_transition, next_state


def _snap(state: ServiceState) -> ServiceSnapshot:
    return ServiceSnapshot(service=ServiceName.BACKEND, state=state)


def test_unknown_to_healthy():
    s = _snap(ServiceState.UNKNOWN)
    assert next_state(s, False, False) == ServiceState.HEALTHY


def test_unknown_to_failed():
    s = _snap(ServiceState.UNKNOWN)
    assert next_state(s, True, False) == ServiceState.FAILED


def test_healthy_to_failed():
    s = _snap(ServiceState.HEALTHY)
    assert next_state(s, True, False) == ServiceState.FAILED


def test_healthy_to_degraded():
    s = _snap(ServiceState.HEALTHY)
    assert next_state(s, False, True) == ServiceState.DEGRADED


def test_failed_to_recovering_via_caller():
    # FAILED stays FAILED; caller promotes to RECOVERING
    s = _snap(ServiceState.FAILED)
    assert next_state(s, True, False) == ServiceState.FAILED
    assert next_state(s, False, False) == ServiceState.RECOVERED


def test_recovering_to_verifying():
    s = _snap(ServiceState.RECOVERING)
    assert next_state(s, False, False) == ServiceState.VERIFYING


def test_verifying_success_to_recovered():
    s = _snap(ServiceState.VERIFYING)
    assert next_state(s, False, False, verifying_success=True) == ServiceState.RECOVERED


def test_verifying_fail_to_failed():
    s = _snap(ServiceState.VERIFYING)
    assert next_state(s, True, False, verifying_success=False) == ServiceState.FAILED


def test_trading_blocked_on_safety():
    s = _snap(ServiceState.FAILED)
    assert next_state(s, False, False, safety_trading_blocked=True) == ServiceState.TRADING_BLOCKED


def test_manual_sticky_until_healthy():
    s = _snap(ServiceState.MANUAL_INTERVENTION_REQUIRED)
    assert next_state(s, True, False) == ServiceState.MANUAL_INTERVENTION_REQUIRED
    assert next_state(s, False, False) == ServiceState.HEALTHY


def test_event_mapping():
    assert event_for_transition(ServiceState.HEALTHY, ServiceState.FAILED) == NotificationEvent.FAILURE
    assert event_for_transition(ServiceState.FAILED, ServiceState.RECOVERING) == NotificationEvent.RECOVERY_STARTED
    assert event_for_transition(ServiceState.VERIFYING, ServiceState.RECOVERED) == NotificationEvent.RECOVERED
    assert event_for_transition(ServiceState.HEALTHY, ServiceState.HEALTHY) is None
    assert event_for_transition(ServiceState.UNKNOWN, ServiceState.HEALTHY) == NotificationEvent.START
