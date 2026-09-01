"""Watchdog daemon — observe, classify, notify, verify, escalate.

Never becomes a second process manager.
Respects ownership:
  systemd -> process_manager + watchdog + demo survival
  process_manager -> gateway/backend/webhook
  watchdog -> observe/notify/verify/escalate
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

from app.services.watchdog.config import WatchdogSettings, get_watchdog_settings
from app.services.watchdog.health import (
    BackendHealthChecker,
    DemoHealthChecker,
    GatewayHealthChecker,
    PostgresHealthChecker,
    RedisHealthChecker,
    WebhookHealthChecker,
)
from app.services.watchdog.models import (
    HealthStatus,
    NotificationEvent,
    ServiceName,
    ServiceSnapshot,
    ServiceState,
)
from app.services.watchdog.notifier import NotificationQueue, format_telegram_message
from app.services.watchdog.recovery_store import RecoveryBudgetStore
from app.services.watchdog.safety import SafetyGateChecker
from app.services.watchdog.state_machine import event_for_transition, next_state
from app.services.watchdog.telegram import TelegramClient

logger = logging.getLogger(__name__)

# Services watchdog monitors (demo failure must not affect trading)
MONITORED_SERVICES = [
    ServiceName.GATEWAY,
    ServiceName.BACKEND,
    ServiceName.WEBHOOK,
    ServiceName.DEMO,
    ServiceName.POSTGRES,
    ServiceName.REDIS,
]

# Trading-critical services that can trigger TRADING_BLOCKED
TRADING_CRITICAL = {ServiceName.GATEWAY, ServiceName.BACKEND, ServiceName.POSTGRES}

# Trading session window — must match process_manager.py SESSION_* (weekdays 09:30-16:00 ET)
# Reused here for market-closed semantics without importing process_manager (avoid circular dep)
_SESSION_TZ = ZoneInfo("America/New_York")
_SESSION_START = dtime(9, 30)
_SESSION_END = dtime(16, 0)
_MARKET_CLOSED_SERVICES = {ServiceName.GATEWAY, ServiceName.BACKEND, ServiceName.WEBHOOK}


def _is_trading_session(now: datetime | None = None) -> bool:
    """Mirror process_manager.is_trading_session — weekdays 09:30-16:00 ET."""
    now_et = (now or datetime.now(_SESSION_TZ)).astimezone(_SESSION_TZ)
    if now_et.weekday() >= 5:
        return False
    return _SESSION_START <= now_et.time() < _SESSION_END


def _is_market_closed_for(svc: ServiceName, now: datetime | None = None) -> bool:
    """True if svc is expected to be stopped because market is closed."""
    if svc not in _MARKET_CLOSED_SERVICES:
        return False
    return not _is_trading_session(now)


class WatchdogDaemon:
    def __init__(self, settings: WatchdogSettings | None = None):
        self.settings = settings or get_watchdog_settings()
        self.telegram = TelegramClient(
            bot_token=self.settings.telegram_bot_token,
            chat_id=self.settings.telegram_chat_id,
            timeout=self.settings.telegram_timeout_seconds,
            max_retries=self.settings.telegram_max_retries,
            rate_limit_per_sec=self.settings.telegram_rate_limit_per_sec,
            enabled=self.settings.telegram_enabled,
        )
        self.notifier = NotificationQueue(self.telegram, self.settings)
        self.safety = SafetyGateChecker(self.settings)
        self.recovery_store = RecoveryBudgetStore(self.settings)

        self.snapshots: dict[ServiceName, ServiceSnapshot] = {
            svc: ServiceSnapshot(service=svc) for svc in MONITORED_SERVICES
        }
        # hydrate persisted recovery attempts (survives restart, fail-closed on corruption)
        try:
            persisted = self.recovery_store.load()
            if self.recovery_store.is_corrupted():
                logger.error("Recovery budget corrupted — failing closed (budget treated as exhausted)")
            for svc in MONITORED_SERVICES:
                lst = persisted.get(svc.value, [])
                self.snapshots[svc].recovery_attempts = lst
        except Exception:
            logger.exception("Failed to load persisted recovery budget")
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

        # health checkers
        self.checkers = {
            ServiceName.GATEWAY: GatewayHealthChecker(self.settings),
            ServiceName.BACKEND: BackendHealthChecker(self.settings),
            ServiceName.WEBHOOK: WebhookHealthChecker(self.settings),
            ServiceName.DEMO: DemoHealthChecker(self.settings),
            ServiceName.POSTGRES: PostgresHealthChecker(self.settings),
            ServiceName.REDIS: RedisHealthChecker(self.settings),
        }

    def _port_for(self, svc: ServiceName) -> int | None:
        mapping = {
            ServiceName.GATEWAY: self.settings.gateway_port,
            ServiceName.BACKEND: self.settings.backend_port,
            ServiceName.WEBHOOK: self.settings.webhook_port,
            ServiceName.DEMO: self.settings.demo_port,
            ServiceName.POSTGRES: self.settings.postgres_port,
            ServiceName.REDIS: self.settings.redis_port,
        }
        return mapping.get(svc)

    def _is_recovery_budget_exhausted(self, snap: ServiceSnapshot) -> bool:
        # persisted check takes precedence (handles restart + corruption fail-closed)
        if self.recovery_store.is_corrupted():
            return True
        # also check in-memory (which was hydrated from persisted)
        cutoff = datetime.now(UTC).timestamp() - self.settings.recovery_window_seconds
        recent = [dt for dt in snap.recovery_attempts if dt.timestamp() > cutoff and dt.timestamp() <= datetime.now(UTC).timestamp() + 60]
        if len(recent) >= self.settings.recovery_max_attempts:
            return True
        # consult persisted store as authoritative (in case of external writes)
        return self.recovery_store.is_exhausted(snap.service.value, self.settings.recovery_max_attempts, self.settings.recovery_window_seconds)

    async def _check_one(self, svc: ServiceName) -> None:
        checker = self.checkers.get(svc)
        if not checker:
            return
        snap = self.snapshots[svc]
        prev_state = snap.state
        try:
            hr = await asyncio.wait_for(checker.check(), timeout=5.0)
        except Exception as exc:
            logger.exception("Health check exception for %s", svc.value)
            from app.services.watchdog.models import HealthResult

            hr = HealthResult(service=svc, status=HealthStatus.FAILED, detail=f"checker exception: {exc}")
        snap.last_health = hr
        health_failed = hr.status == HealthStatus.FAILED
        health_degraded = hr.status == HealthStatus.DEGRADED

        # Update consecutive counters
        if health_failed:
            snap.consecutive_failures += 1
            snap.consecutive_successes = 0
            snap.failure_reason = hr.underlying_error or hr.detail
        elif health_degraded:
            snap.consecutive_failures += 1
            snap.consecutive_successes = 0
        else:
            snap.consecutive_successes += 1
            if snap.consecutive_successes >= 1:
                snap.consecutive_failures = 0

        # Market-closed semantics: expected stop outside trading window (weekdays 09:30-16:00 ET)
        is_market_closed = _is_market_closed_for(svc) if self.settings.market_closed_enabled else False
        # If market closed and health shows stopped, treat as MARKET_CLOSED not FAILED
        # Don't count safety gates when market is closed — trading is intentionally unavailable
        if is_market_closed and health_failed:
            # Override failure reason to honest expected-stop message (don't count as failure)
            snap.failure_reason = "outside trading window 09:30-16:00 ET (market closed) – service intentionally stopped"

        # Safety gate: trading-critical services must have all gates SAFE to be READY
        # Check whenever health is healthy, not only after failure, to avoid claiming READY when blocked
        # Skip safety check when market is closed — no trading anyway
        safety_blocked = False
        if not is_market_closed and svc in TRADING_CRITICAL and not health_failed and not health_degraded:
            # Always verify safety gates for trading-critical healthy checks
            # To avoid excessive load, we still check every time (cheap HTTP), but could cache briefly
            gate = await self.safety.check()
            if not gate.passed:
                safety_blocked = True
                snap.failure_reason = "safety gate: " + "; ".join(gate.failures)
            else:
                # gates passed — clear any previous safety failure reason if it was stale
                if snap.failure_reason.startswith("safety gate:"):
                    snap.failure_reason = ""

        # State transition — with safety_blocked and market_closed considered
        nxt = next_state(snap, health_failed=health_failed, health_degraded=health_degraded, safety_trading_blocked=safety_blocked, is_market_closed=is_market_closed)
        # If safety blocked but state machine returned HEALTHY (should not happen after our change),
        # ensure we go to TRADING_BLOCKED
        if safety_blocked and nxt == ServiceState.HEALTHY:
            nxt = ServiceState.TRADING_BLOCKED
        # If market closed, never go to FAILED/RECOVERING — ensures no recovery attempts during expected downtime
        if is_market_closed and health_failed and nxt in (ServiceState.FAILED, ServiceState.RECOVERING, ServiceState.VERIFYING):
            nxt = ServiceState.MARKET_CLOSED

        # Recovery budget handling: FAILED -> RECOVERING or MANUAL
        # Record attempt BEFORE emitting RECOVERY_STARTED so counter is 1/5 not 0/5
        if prev_state == ServiceState.FAILED and nxt == ServiceState.FAILED:
            # still failed — decide to enter RECOVERING or MANUAL (second consecutive poll)
            if snap.state == ServiceState.FAILED:
                # First detection of prolonged failure
                if not self._is_recovery_budget_exhausted(snap):
                    nxt = ServiceState.RECOVERING
                    # record attempt now for accurate counter
                    now = datetime.now(UTC)
                    snap.recovery_attempts.append(now)
                    snap.last_recovery_at = now
                    try:
                        state = {k.value: v.recovery_attempts for k, v in self.snapshots.items()}
                        self.recovery_store.save(state)
                    except Exception:
                        logger.exception("Failed to persist recovery budget")
                else:
                    nxt = ServiceState.MANUAL_INTERVENTION_REQUIRED
        elif nxt == ServiceState.FAILED and prev_state not in (ServiceState.FAILED, ServiceState.RECOVERING, ServiceState.VERIFYING, ServiceState.TRADING_BLOCKED):
            # new failure — emit FAILURE first, recovery will be next loop
            pass
        # Also handle direct FAILED->RECOVERING transition that wasn't captured by "still failed" above
        # e.g., HEALTHY->FAILED on this poll, nxt is FAILED, but we want to immediately consider recovery?
        # We emit FAILURE first, then next poll will promote to RECOVERING, so no immediate attempt.
        # However if nxt == RECOVERING via safety_blocked path or budget, record attempt
        if prev_state == ServiceState.FAILED and nxt == ServiceState.RECOVERING:  # noqa: SIM102
            # Ensure attempt recorded if not already (e.g., initial promotion from FAILED)
            if not snap.recovery_attempts or (datetime.now(UTC) - snap.recovery_attempts[-1]).total_seconds() > 1:  # noqa: SIM102
                # avoid double count if just recorded above
                if not (snap.last_recovery_at and (datetime.now(UTC) - snap.last_recovery_at).total_seconds() < 0.5):
                    now = datetime.now(UTC)
                    snap.recovery_attempts.append(now)
                    snap.last_recovery_at = now
                    try:
                        state = {k.value: v.recovery_attempts for k, v in self.snapshots.items()}
                        self.recovery_store.save(state)
                    except Exception:
                        logger.exception("Failed to persist recovery budget")

        # Handle RECOVERING -> VERIFYING -> outcome
        verifying_success: bool | None = None
        if nxt == ServiceState.VERIFYING or snap.state == ServiceState.RECOVERING:
            # We are in recovering/verifying — perform verification
            # For gateway/backend, verification is re-check after short wait + safety
            # Simple: if current health is healthy, success
            if not health_failed and not health_degraded and not safety_blocked:
                verifying_success = True
            elif health_failed:
                verifying_success = False

            if snap.state == ServiceState.RECOVERING:
                nxt = ServiceState.VERIFYING
                # If attempt not yet recorded for this recovery cycle (when came from FAILED->RECOVERING->VERIFYING without intermediate poll)
                # Check if last attempt is stale (> interval)
                if not snap.last_recovery_at or (datetime.now(UTC) - snap.last_recovery_at).total_seconds() > 2:
                    now = datetime.now(UTC)
                    snap.recovery_attempts.append(now)
                    snap.last_recovery_at = now
                    try:
                        state = {k.value: v.recovery_attempts for k, v in self.snapshots.items()}
                        self.recovery_store.save(state)
                    except Exception:
                        logger.exception("Failed to persist recovery budget")
            elif snap.state == ServiceState.VERIFYING:
                # compute final
                tmp = ServiceSnapshot(service=svc, state=ServiceState.VERIFYING)
                nxt2 = next_state(tmp, health_failed, health_degraded, verifying_success=verifying_success, safety_trading_blocked=safety_blocked)
                if nxt2 == ServiceState.FAILED and verifying_success is False:
                    if self._is_recovery_budget_exhausted(snap):
                        nxt = ServiceState.MANUAL_INTERVENTION_REQUIRED
                    else:
                        nxt = ServiceState.RECOVERING
                        # new recovery attempt will be recorded on next loop when entering VERIFYING again
                else:
                    nxt = nxt2

        # PID change tracking — diagnostic only, not a failure trigger
        # Store pid for comparison; if pid changed but health healthy, just log, don't emit failure
        if hr.pid is not None and snap.last_health and snap.last_health.pid is not None and hr.pid != snap.last_health.pid and not health_failed and not health_degraded:
            logger.info("WATCHDOG: service=%s PID changed %s -> %s (healthy, not a failure)", svc.value, snap.last_health.pid, hr.pid)

        if nxt != prev_state:
            transition_at = datetime.now(UTC)
            snap.state = nxt
            snap.last_transition_at = transition_at
            # notify
            event = event_for_transition(prev_state, nxt)
            # also handle FAILED->RECOVERING direct
            if prev_state == ServiceState.FAILED and nxt == ServiceState.RECOVERING:
                event = NotificationEvent.RECOVERY_STARTED
            elif prev_state == ServiceState.FAILED and nxt == ServiceState.MANUAL_INTERVENTION_REQUIRED:
                event = NotificationEvent.MANUAL_INTERVENTION_REQUIRED
            elif nxt == ServiceState.TRADING_BLOCKED:
                event = NotificationEvent.TRADING_BLOCKED
            elif prev_state == ServiceState.TRADING_BLOCKED and nxt in (ServiceState.HEALTHY, ServiceState.RECOVERED):
                # unblocked — treat as recovered for operator clarity
                event = NotificationEvent.RECOVERED
            if event:
                port = self._port_for(svc)
                attempt_str = None
                recovery_duration = None
                if nxt in (ServiceState.RECOVERING, ServiceState.VERIFYING, ServiceState.MANUAL_INTERVENTION_REQUIRED, ServiceState.RECOVERED, ServiceState.TRADING_BLOCKED):
                    cutoff = datetime.now(UTC).timestamp() - self.settings.recovery_window_seconds
                    recent = sum(1 for t in snap.recovery_attempts if t.timestamp() > cutoff)
                    # For RECOVERING, ensure at least 1/5 if we just recorded
                    if nxt == ServiceState.RECOVERING and recent == 0:
                        recent = 1
                    attempt_str = f"{recent}/{self.settings.recovery_max_attempts}"
                    if nxt == ServiceState.RECOVERED and snap.last_recovery_at:
                        recovery_duration = (transition_at - snap.last_recovery_at).total_seconds()
                text = format_telegram_message(
                    service=svc,
                    event=event,
                    snapshot=snap,
                    host=self.settings.watchdog_host,
                    port=port,
                    attempt=attempt_str,
                    reason=snap.failure_reason or hr.detail,
                    health=hr,
                    recovery_duration=recovery_duration,
                    event_timestamp=transition_at,
                )
                # Don't block on telegram failure; log delivery attempt with ordering info
                try:
                    enqueued = self.notifier.enqueue(svc, event, text)
                    if not enqueued:
                        logger.warning("WATCHDOG: notification dedup suppressed service=%s event=%s", svc.value, event.value)
                    else:
                        logger.info(
                            "WATCHDOG: service=%s prev=%s next=%s event=%s reason=%s attempt=%s ts=%s",
                            svc.value,
                            prev_state.value,
                            nxt.value,
                            event.value if event else "none",
                            snap.failure_reason[:200],
                            attempt_str or "-",
                            transition_at.isoformat(),
                        )
                except Exception:
                    logger.exception("Failed to enqueue notification for %s event=%s ts=%s", svc.value, event.value if event else "none", transition_at.isoformat())
            # Clear failure reason and dedup on recovery
            if nxt in (ServiceState.HEALTHY, ServiceState.RECOVERED):
                snap.failure_reason = ""
                snap.recovery_failed_count = 0
                # Allow next failure to notify promptly even within previous cooldown
                try:
                    self.notifier.dedup.clear(svc)
                except Exception:
                    pass

    async def _loop(self) -> None:
        logger.info("Watchdog daemon started host=%s interval=%.1fs", self.settings.watchdog_host, self.settings.watchdog_interval_seconds)
        # initial START notifications
        for svc in MONITORED_SERVICES:
            snap = self.snapshots[svc]
            if snap.state == ServiceState.UNKNOWN:
                snap.state = ServiceState.STARTING
        while not self._stop.is_set():
            for svc in MONITORED_SERVICES:
                try:
                    await self._check_one(svc)
                except Exception:
                    logger.exception("Watchdog check failed for %s", svc.value)
                # Demo failure must not affect trading — already isolated via separate state
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.watchdog_interval_seconds)
            except TimeoutError:
                continue

    async def start(self) -> None:
        await self.notifier.start()
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError:
                self._task.cancel()
        await self.notifier.stop()

    def run_forever(self) -> None:
        async def _run():
            await self.start()
            await self._stop.wait()
        asyncio.run(_run())
