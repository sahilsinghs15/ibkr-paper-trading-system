"""Watchdog state machine tests."""

from app.services.watchdog.models import (
    NotificationEvent,
    ServiceName,
    ServiceSnapshot,
    ServiceState,
)
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


# Regression for Phase 2C: MANUAL → HEALTHY must notify RECOVERED (demo was stuck without notification)
def test_manual_to_healthy_returns_recovered():
    assert event_for_transition(ServiceState.MANUAL_INTERVENTION_REQUIRED, ServiceState.HEALTHY) == NotificationEvent.RECOVERED


def test_manual_to_recovered_is_not_a_state_transition():
    # RECOVERED is both a ServiceState and NotificationEvent, but MANUAL never transitions directly to RECOVERED state
    # (it goes to HEALTHY). MANUAL->RECOVERED as state should not generate an event — the valid path is MANUAL->HEALTHY.
    # This test documents that we intentionally do NOT invent MANUAL->RECOVERED.
    assert event_for_transition(ServiceState.MANUAL_INTERVENTION_REQUIRED, ServiceState.RECOVERED) is None


def test_recovering_to_verifying_unchanged():
    s = _snap(ServiceState.RECOVERING)
    assert next_state(s, False, False) == ServiceState.VERIFYING
    assert event_for_transition(ServiceState.RECOVERING, ServiceState.VERIFYING) is None


def test_failed_to_recovered_unchanged():
    assert event_for_transition(ServiceState.FAILED, ServiceState.RECOVERED) == NotificationEvent.RECOVERED


def test_starting_to_healthy_start_unchanged():
    assert event_for_transition(ServiceState.STARTING, ServiceState.HEALTHY) == NotificationEvent.START
