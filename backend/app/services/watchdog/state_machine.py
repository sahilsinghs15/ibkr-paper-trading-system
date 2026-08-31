"""Deterministic state machine for watchdog.

Transitions are pure functions; no I/O.
"""

from __future__ import annotations

from app.services.watchdog.models import (
    NotificationEvent,
    ServiceSnapshot,
    ServiceState,
)


def next_state(
    snapshot: ServiceSnapshot,
    health_failed: bool,
    health_degraded: bool,
    verifying_success: bool | None = None,
    safety_trading_blocked: bool = False,
) -> ServiceState:
    """Compute next state deterministically.

    verifying_success is only relevant when current state is VERIFYING.
    safety_trading_blocked forces TRADING_BLOCKED if true and service is trading-critical.
    """
    cur = snapshot.state

    # Safety gate overrides
    if safety_trading_blocked and cur not in (ServiceState.MANUAL_INTERVENTION_REQUIRED, ServiceState.TRADING_BLOCKED):
        # Only trading-critical services should enter TRADING_BLOCKED (gateway/backend)
        return ServiceState.TRADING_BLOCKED

    if cur == ServiceState.UNKNOWN:
        if health_failed:
            return ServiceState.FAILED
        if health_degraded:
            return ServiceState.DEGRADED
        return ServiceState.HEALTHY

    if cur == ServiceState.STARTING:
        if health_failed:
            return ServiceState.FAILED
        if health_degraded:
            return ServiceState.DEGRADED
        return ServiceState.HEALTHY

    if cur == ServiceState.HEALTHY:
        if health_failed:
            return ServiceState.FAILED
        if health_degraded:
            return ServiceState.DEGRADED
        return ServiceState.HEALTHY

    if cur == ServiceState.DEGRADED:
        if health_failed:
            return ServiceState.FAILED
        if not health_failed and not health_degraded:
            return ServiceState.HEALTHY
        return ServiceState.DEGRADED

    if cur == ServiceState.FAILED:
        # Will be moved to RECOVERING by recovery manager externally; state machine alone stays FAILED
        # But if health recovers spontaneously, go to RECOVERED/HEALTHY
        if not health_failed and not health_degraded:
            return ServiceState.RECOVERED
        if not health_failed and health_degraded:
            return ServiceState.DEGRADED
        return ServiceState.FAILED

    if cur == ServiceState.RECOVERING:
        # Next is VERIFYING (done by caller)
        return ServiceState.VERIFYING

    if cur == ServiceState.VERIFYING:
        if verifying_success is True:
            return ServiceState.RECOVERED
        if verifying_success is False:
            return ServiceState.FAILED  # will be retried or escalated by policy
        # still verifying
        return ServiceState.VERIFYING

    if cur == ServiceState.RECOVERED:
        if health_failed:
            return ServiceState.FAILED
        if health_degraded:
            return ServiceState.DEGRADED
        return ServiceState.HEALTHY

    if cur in (ServiceState.MANUAL_INTERVENTION_REQUIRED, ServiceState.TRADING_BLOCKED):
        # Sticky until operator clears or health recovers + manual reset externally
        # For now, if health becomes healthy, move to HEALTHY (operator cleared)
        if not health_failed and not health_degraded:
            return ServiceState.HEALTHY
        return cur

    return cur


def event_for_transition(prev: ServiceState, nxt: ServiceState) -> NotificationEvent | None:
    """Map state transition to notification event."""
    if prev == nxt:
        return None
    mapping: dict[tuple[ServiceState, ServiceState], NotificationEvent] = {
        (ServiceState.UNKNOWN, ServiceState.HEALTHY): NotificationEvent.START,
        (ServiceState.STARTING, ServiceState.HEALTHY): NotificationEvent.START,
        (ServiceState.UNKNOWN, ServiceState.FAILED): NotificationEvent.FAILURE,
        (ServiceState.HEALTHY, ServiceState.FAILED): NotificationEvent.FAILURE,
        (ServiceState.DEGRADED, ServiceState.FAILED): NotificationEvent.FAILURE,
        (ServiceState.HEALTHY, ServiceState.DEGRADED): NotificationEvent.UNHEALTHY,
        (ServiceState.FAILED, ServiceState.RECOVERING): NotificationEvent.RECOVERY_STARTED,
        (ServiceState.VERIFYING, ServiceState.RECOVERED): NotificationEvent.RECOVERED,
        (ServiceState.VERIFYING, ServiceState.FAILED): NotificationEvent.RECOVERY_FAILED,
        (ServiceState.FAILED, ServiceState.MANUAL_INTERVENTION_REQUIRED): NotificationEvent.MANUAL_INTERVENTION_REQUIRED,
        (ServiceState.RECOVERING, ServiceState.MANUAL_INTERVENTION_REQUIRED): NotificationEvent.MANUAL_INTERVENTION_REQUIRED,
        (ServiceState.VERIFYING, ServiceState.MANUAL_INTERVENTION_REQUIRED): NotificationEvent.MANUAL_INTERVENTION_REQUIRED,
    }
    # Generic fallback for TRADING_BLOCKED
    if nxt == ServiceState.TRADING_BLOCKED:
        return NotificationEvent.TRADING_BLOCKED
    if prev == ServiceState.FAILED and nxt == ServiceState.RECOVERED:
        return NotificationEvent.RECOVERED
    return mapping.get((prev, nxt))


def is_trading_critical(service_name: str) -> bool:
    return service_name in ("gateway", "backend")
