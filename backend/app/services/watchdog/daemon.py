"""Watchdog daemon — observe, classify, notify, verify, escalate.

Never becomes a second process manager.
Respects ownership:
  systemd -> ibgateway + trading-backend + webhook + demo + watchdog (each Restart=always)
  ibgateway (Xvfb+Gateway) --every START--> trading-backend (via trigger) --on healthy--> demo (one-way)
  webhook -- independent, trading-hours only
  trading-backend -- 24/7, triggers demo on each restart
  watchdog -> observe/notify/verify/escalate (no systemctl, no process_manager)
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
from app.services.watchdog.notifier import (
    NotificationQueue,
    format_resource_alert,
    format_telegram_message,
)
from app.services.watchdog.recovery_store import RecoveryBudgetStore
from app.services.watchdog.resources import ResourceMonitor, ResourceState
from app.services.watchdog.safety import SafetyGateChecker
from app.services.watchdog.state_machine import event_for_transition, next_state
from app.services.watchdog.status import build_status_message
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
# New architecture: only IB Gateway and Webhook follow market hours; Backend is 24/7, Demo/Postgres/Redis independent
_SESSION_TZ = ZoneInfo("America/New_York")
_SESSION_START = dtime(9, 30)
_SESSION_END = dtime(16, 0)
_MARKET_CLOSED_SERVICES = {ServiceName.GATEWAY, ServiceName.WEBHOOK}


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
        self._command_task: asyncio.Task | None = None
        self._telegram_offset: int | None = None
        self.resource_monitor = ResourceMonitor(self.settings)
        self.safety_blocked: bool = False
        self.last_safety_gate_result: SafetyGateResult | None = None

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

        # Backend 24/7: When market is closed, TWS disconnection due to intentionally stopped Gateway is expected, not a failure/DEGRADED
        if svc == ServiceName.BACKEND and health_degraded:
            global_closed_for_backend = (not _is_trading_session()) if self.settings.market_closed_enabled else False
            if global_closed_for_backend:
                reason_lower = (hr.reason or "").lower()
                detail_lower = (hr.detail or "").lower()
                underlying_lower = (hr.underlying_error or "").lower()
                if reason_lower in ("readiness_failed", "readiness_degraded", "tws_disconnected") or "tws" in detail_lower or "tws" in underlying_lower or "readiness" in detail_lower or "readiness" in underlying_lower:
                    logger.info("Market closed — treating Backend TWS disconnected as expected HEALTHY (not DEGRADED): %s", hr.detail[:100])
                    health_degraded = False
                    hr.status = HealthStatus.HEALTHY
                    hr.liveness = HealthStatus.HEALTHY
                    hr.readiness = HealthStatus.HEALTHY
                    hr.reason = "healthy_market_closed"
                    hr.what_happened = "Trading Backend is running 24/7; TWS connectivity is unavailable because IB Gateway is intentionally stopped outside the trading session."
                    hr.impact = "Backend remains running; trading execution is paused outside session."
                    hr.trading_impact = "Market closed — no trading expected."
                    hr.underlying_error = None
                    hr.detail = "Healthy (market closed, TWS expected unavailable)"

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
        global_market_closed = (not _is_trading_session()) if self.settings.market_closed_enabled else False
        # If market closed and health shows stopped, treat as MARKET_CLOSED not FAILED
        # Don't count safety gates when market is closed — trading is intentionally unavailable
        if is_market_closed and health_failed:
            # Override failure reason to honest expected-stop message (don't count as failure)
            snap.failure_reason = "outside trading window 09:30-16:00 ET (market closed) – service intentionally stopped"

        # State transition — honest service health (not mutated by safety gate)
        nxt = next_state(
            snap,
            health_failed=health_failed,
            health_degraded=health_degraded,
            safety_trading_blocked=False,
            is_market_closed=is_market_closed,
        )
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
            if not health_failed and not health_degraded and not self.safety_blocked:
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
                nxt2 = next_state(tmp, health_failed, health_degraded, verifying_success=verifying_success, safety_trading_blocked=self.safety_blocked)
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

    async def _check_resources(self) -> None:
        """Host resource checks with hysteresis — only notify on transitions."""
        if not self.resource_monitor.check_if_due():
            return
        results = self.resource_monitor.check_all()
        for res in results:
            if not res.is_transition:
                continue
            # Determine if this is recovery (NORMAL) or warning/critical
            is_recovery = res.state == ResourceState.NORMAL and res.previous_state in (ResourceState.WARNING, ResourceState.CRITICAL)
            # Threshold for message
            thr = 0.0
            try:
                from app.services.watchdog.resources import _get_thresholds
                t = _get_thresholds(self.settings, res.type)
                if res.state == ResourceState.CRITICAL:
                    thr = t.critical
                elif res.state == ResourceState.WARNING:
                    thr = t.warning
                else:
                    # Recovery threshold
                    thr = t.recovery
            except Exception:
                thr = 80.0
            # Build resource alert
            text = format_resource_alert(
                resource_type=res.type.value,
                state=res.state.value,
                usage_percent=res.metrics.usage_percent,
                threshold=thr,
                total_bytes=res.metrics.total_bytes,
                used_bytes=res.metrics.used_bytes,
                available_bytes=res.metrics.available_bytes,
                mount=res.metrics.extra.get("mount") if res.metrics.extra else None,
                extra=res.metrics.extra,
                is_recovery=is_recovery,
            )
            # Use appropriate priority: critical → critical, warning → warning, recovery → info
            # Map to ServiceName for queue — use a pseudo-service for resources
            pseudo_service = ServiceName.GATEWAY  # placeholder for queue bucket, but text indicates resource
            # Instead, use a dedicated resource service name if available, fallback to gateway for critical path
            # To avoid mixing with trading critical, we enqueue with force to ensure delivery
            event = NotificationEvent.FAILURE if res.state == ResourceState.CRITICAL else (NotificationEvent.UNHEALTHY if res.state == ResourceState.WARNING else NotificationEvent.RECOVERED)
            # For resources, use ServiceName.POSTGRES as placeholder for infrastructure? Use gateway for now but with resource text
            # Better to use ServiceName.REDIS for non-critical? Keep simple: use ServiceName.GATEWAY for all resources but dedup key will be per-resource via text
            try:
                # Use force to ensure resource transitions are not deduped as trading events
                self.notifier.enqueue(pseudo_service, event, text, force=True)
                logger.info("WATCHDOG RESOURCE: %s %s %.1f%% threshold %.1f%%", res.type.value, res.state.value, res.metrics.usage_percent, thr)
            except Exception:
                logger.exception("Failed to enqueue resource alert for %s", res.type.value)

    async def _command_loop(self) -> None:
        """Poll Telegram for /status commands (read-only)."""
        if not self.telegram.configured:
            return
        while not self._stop.is_set():
            try:
                updates = await self.telegram.get_updates(offset=self._telegram_offset, timeout=10)
                for upd in updates:
                    try:
                        upd_id = upd.get("update_id")
                        if upd_id is not None:
                            self._telegram_offset = max(self._telegram_offset or 0, upd_id + 1)
                        msg = upd.get("message") or upd.get("edited_message") or {}
                        text = (msg.get("text") or "").strip()
                        chat = msg.get("chat") or {}
                        chat_id = str(chat.get("id") or "")
                        # Only respond to configured chat and /status
                        if not text or not chat_id:
                            continue
                        # Restrict to configured chat_id if set
                        expected_chat = str(self.settings.telegram_chat_id or "")
                        if expected_chat and chat_id != expected_chat:
                            continue
                        if text.split()[0] in ("/status", "/status@"+ (self.telegram.bot_token.split(":")[0] if self.telegram.bot_token else "")):
                            # Build status (read-only, no side effects)
                            status_text = build_status_message(
                                snapshots=self.snapshots,
                                resource_monitor=self.resource_monitor,
                                settings=self.settings,
                            )
                            # Reply to same chat (override chat_id temporarily)
                            orig_chat = self.telegram.chat_id
                            self.telegram.chat_id = chat_id
                            try:
                                await self.telegram.send_message(status_text)
                                logger.info("WATCHDOG /status served to chat %s", chat_id)
                            finally:
                                self.telegram.chat_id = orig_chat
                    except Exception:
                        logger.exception("Failed to handle telegram update %s", upd.get("update_id"))
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Telegram command poll failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=2.0)
            except TimeoutError:
                continue

    async def _check_safety_gates(self) -> None:
        """Evaluate system trading safety gates once per loop and notify on transitions."""
        global_market_closed = (not _is_trading_session()) if self.settings.market_closed_enabled else False
        if global_market_closed:
            return

        try:
            gate = await self.safety.check()
        except Exception:
            logger.exception("Failed to check safety gates")
            return

        self.last_safety_gate_result = gate

        if not gate.passed:
            if not self.safety_blocked:
                self.safety_blocked = True
                reason_str = "; ".join(gate.failures) if gate.failures else gate.details
                text = format_telegram_message(
                    service=ServiceName.GATEWAY,
                    event=NotificationEvent.TRADING_BLOCKED,
                    snapshot=ServiceSnapshot(service=ServiceName.GATEWAY, state=ServiceState.TRADING_BLOCKED, failure_reason=reason_str),
                    host=self.settings.watchdog_host,
                    port=self.settings.gateway_port,
                    reason=reason_str,
                )
                self.notifier.enqueue(ServiceName.GATEWAY, NotificationEvent.TRADING_BLOCKED, text, force=True)
                logger.warning("WATCHDOG SAFETY GATE BLOCKED: %s", reason_str)
        else:
            if self.safety_blocked:
                self.safety_blocked = False
                reason_str = "All trading safety gates SAFE."
                text = format_telegram_message(
                    service=ServiceName.GATEWAY,
                    event=NotificationEvent.RECOVERED,
                    snapshot=ServiceSnapshot(service=ServiceName.GATEWAY, state=ServiceState.HEALTHY, failure_reason=""),
                    host=self.settings.watchdog_host,
                    port=self.settings.gateway_port,
                    reason=reason_str,
                )
                self.notifier.enqueue(ServiceName.GATEWAY, NotificationEvent.RECOVERED, text, force=True)
                logger.info("WATCHDOG SAFETY GATE CLEARED: %s", reason_str)

    async def _check_services(self) -> None:
        """Run health checks for all services and evaluate safety gates."""
        for svc in MONITORED_SERVICES:
            try:
                await self._check_one(svc)
            except Exception:
                logger.exception("Watchdog check failed for %s", svc.value)
        try:
            await self._check_safety_gates()
        except Exception:
            logger.exception("Watchdog safety gate check failed")

    async def _loop(self) -> None:
        logger.info("Watchdog daemon started host=%s interval=%.1fs", self.settings.watchdog_host, self.settings.watchdog_interval_seconds)
        # initial START notifications
        for svc in MONITORED_SERVICES:
            snap = self.snapshots[svc]
            if snap.state == ServiceState.UNKNOWN:
                snap.state = ServiceState.STARTING
        while not self._stop.is_set():
            try:
                await self._check_services()
            except Exception:
                logger.exception("Watchdog check services failed")
            # Host resources (CPU/RAM/disk/inodes) — hysteresis, only on transition
            try:
                await self._check_resources()
            except Exception:
                logger.exception("Watchdog resource check failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.watchdog_interval_seconds)
            except TimeoutError:
                continue

    async def start(self) -> None:
        await self.notifier.start()
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())
        # Telegram /status poller (read-only, observer)
        if self.telegram.configured:
            self._command_task = asyncio.create_task(self._command_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError:
                self._task.cancel()
        if self._command_task:
            try:
                await asyncio.wait_for(self._command_task, timeout=5.0)
            except TimeoutError:
                self._command_task.cancel()
        await self.notifier.stop()

    def run_forever(self) -> None:
        async def _run():
            await self.start()
            await self._stop.wait()
        asyncio.run(_run())
