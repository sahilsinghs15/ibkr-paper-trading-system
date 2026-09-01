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
    is_market_closed: bool = False,
) -> ServiceState:
    """Compute next state deterministically.

    verifying_success is only relevant when current state is VERIFYING.
    safety_trading_blocked forces TRADING_BLOCKED if true and service is trading-critical.
    is_market_closed indicates services are expected to be stopped outside trading window.
    """
    cur = snapshot.state

    # Market-closed overrides: expected stop, not failure (only for session-aware services)
    # Caller should only set is_market_closed for gateway/backend/webhook; we enforce here
    if is_market_closed:
        # If service is expected to be stopped and health shows failed (tcp refused), treat as MARKET_CLOSED
        if health_failed:
            # From any non-market-closed state, transition to MARKET_CLOSED instead of FAILED
            if cur != ServiceState.MARKET_CLOSED:
                return ServiceState.MARKET_CLOSED
            return ServiceState.MARKET_CLOSED
        # If health is healthy outside session (admin kept running), stay HEALTHY but don't consider failure
        # If currently MARKET_CLOSED and health becomes healthy (session opened), go to HEALTHY
        if cur == ServiceState.MARKET_CLOSED and not health_failed and not health_degraded:
            return ServiceState.HEALTHY

    # Safety gate overrides
    if safety_trading_blocked and cur not in (ServiceState.MANUAL_INTERVENTION_REQUIRED, ServiceState.TRADING_BLOCKED, ServiceState.MARKET_CLOSED):
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
        # Sticky until operator clears or health recovers + safety passes
        # If safety still blocked, remain blocked even if health healthy
        if safety_trading_blocked:
            return cur
        if not health_failed and not health_degraded:
            return ServiceState.HEALTHY
        return cur

    if cur == ServiceState.MARKET_CLOSED:
        # Market closed is honest expected stop — stay there while health failed and market closed
        if is_market_closed and health_failed:
            return ServiceState.MARKET_CLOSED
        if not health_failed and not health_degraded:
            # Service became healthy (session opened) → back to healthy
            return ServiceState.HEALTHY
        if not is_market_closed and health_failed:
            # Market opened but service still down → this is now a real failure, not scheduled
            return ServiceState.FAILED
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
        (ServiceState.MANUAL_INTERVENTION_REQUIRED, ServiceState.HEALTHY): NotificationEvent.RECOVERED,
    }
    # Generic fallback for TRADING_BLOCKED
    if nxt == ServiceState.TRADING_BLOCKED:
        return NotificationEvent.TRADING_BLOCKED
    if nxt == ServiceState.MARKET_CLOSED:
        return NotificationEvent.MARKET_CLOSED
    if prev == ServiceState.MARKET_CLOSED and nxt == ServiceState.HEALTHY:
        return NotificationEvent.RECOVERED
    if prev == ServiceState.MARKET_CLOSED and nxt == ServiceState.FAILED:
        return NotificationEvent.FAILURE
    if prev == ServiceState.TRADING_BLOCKED and nxt == ServiceState.HEALTHY:
        return NotificationEvent.RECOVERED
    if prev == ServiceState.TRADING_BLOCKED and nxt == ServiceState.RECOVERED:
        return NotificationEvent.RECOVERED
    if prev == ServiceState.FAILED and nxt == ServiceState.RECOVERED:
        return NotificationEvent.RECOVERED
    return mapping.get((prev, nxt))


def is_trading_critical(service_name: str) -> bool:
    return service_name in ("gateway", "backend")
