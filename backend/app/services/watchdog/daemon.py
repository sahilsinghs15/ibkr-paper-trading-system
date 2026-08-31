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

        # Safety gate: if trading-critical and health shows recovered but gate fails, go TRADING_BLOCKED
        safety_blocked = False
        if svc in TRADING_CRITICAL and not health_failed and not health_degraded:
            # only check gates when service appears healthy after a failure
            if prev_state in (ServiceState.FAILED, ServiceState.RECOVERING, ServiceState.VERIFYING):
                gate = await self.safety.check()
                if not gate.passed:
                    safety_blocked = True
                    snap.failure_reason = "safety gate: " + "; ".join(gate.failures)

        # State transition
        nxt = next_state(snap, health_failed=health_failed, health_degraded=health_degraded, safety_trading_blocked=safety_blocked)

        # Recovery budget handling: FAILED -> RECOVERING or MANUAL
        if prev_state == ServiceState.FAILED and nxt == ServiceState.FAILED:
            # still failed — decide to enter RECOVERING or MANUAL
            if snap.state == ServiceState.FAILED:
                # First detection of prolonged failure
                if not self._is_recovery_budget_exhausted(snap):
                    nxt = ServiceState.RECOVERING
                else:
                    nxt = ServiceState.MANUAL_INTERVENTION_REQUIRED
        elif nxt == ServiceState.FAILED and prev_state not in (ServiceState.FAILED, ServiceState.RECOVERING, ServiceState.VERIFYING):
            # new failure — immediately mark recovering if budget allows
            # but let state machine emit FAILURE first, then next loop will move to RECOVERING
            pass

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
                # record attempt (persist atomically, fail-closed on error)
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
                else:
                    nxt = nxt2

        if nxt != prev_state:
            snap.state = nxt
            snap.last_transition_at = datetime.now(UTC)
            # notify
            event = event_for_transition(prev_state, nxt)
            # also handle FAILED->RECOVERING direct
            if prev_state == ServiceState.FAILED and nxt == ServiceState.RECOVERING:
                event = NotificationEvent.RECOVERY_STARTED
            elif prev_state == ServiceState.FAILED and nxt == ServiceState.MANUAL_INTERVENTION_REQUIRED:
                event = NotificationEvent.MANUAL_INTERVENTION_REQUIRED
            elif nxt == ServiceState.TRADING_BLOCKED:
                event = NotificationEvent.TRADING_BLOCKED
            if event:
                port = self._port_for(svc)
                attempt_str = None
                recovery_duration = None
                if nxt in (ServiceState.RECOVERING, ServiceState.VERIFYING, ServiceState.MANUAL_INTERVENTION_REQUIRED, ServiceState.RECOVERED):
                    cutoff = datetime.now(UTC).timestamp() - self.settings.recovery_window_seconds
                    recent = sum(1 for t in snap.recovery_attempts if t.timestamp() > cutoff)
                    attempt_str = f"{recent}/{self.settings.recovery_max_attempts}"
                    if nxt == ServiceState.RECOVERED and snap.last_recovery_at:
                        recovery_duration = (datetime.now(UTC) - snap.last_recovery_at).total_seconds()
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
                )
                # Don't block on telegram failure
                try:
                    self.notifier.enqueue(svc, event, text)
                except Exception:
                    logger.exception("Failed to enqueue notification for %s", svc.value)
                logger.info(
                    "WATCHDOG: service=%s prev=%s next=%s event=%s reason=%s attempt=%s",
                    svc.value,
                    prev_state.value,
                    nxt.value,
                    event.value if event else "none",
                    snap.failure_reason[:200],
                    attempt_str or "-",
                )
            # Clear failure reason on recovery
            if nxt in (ServiceState.HEALTHY, ServiceState.RECOVERED):
                snap.failure_reason = ""
                snap.recovery_failed_count = 0

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
