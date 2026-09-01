"""Watchdog state models and enums."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


class ServiceName(str, Enum):
    GATEWAY = "gateway"
    BACKEND = "backend"
    WEBHOOK = "webhook"
    DEMO = "demo"
    POSTGRES = "postgres"
    REDIS = "redis"


class ServiceState(str, Enum):
    UNKNOWN = "UNKNOWN"
    STARTING = "STARTING"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    RECOVERING = "RECOVERING"
    VERIFYING = "VERIFYING"
    RECOVERED = "RECOVERED"
    MANUAL_INTERVENTION_REQUIRED = "MANUAL_INTERVENTION_REQUIRED"
    TRADING_BLOCKED = "TRADING_BLOCKED"
    MARKET_CLOSED = "MARKET_CLOSED"


class HealthStatus(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class NotificationEvent(str, Enum):
    START = "START"
    STOP = "STOP"
    FAILURE = "FAILURE"
    UNHEALTHY = "UNHEALTHY"
    RECOVERY_STARTED = "RECOVERY_STARTED"
    RECOVERED = "RECOVERED"
    RECOVERY_FAILED = "RECOVERY_FAILED"
    MANUAL_INTERVENTION_REQUIRED = "MANUAL_INTERVENTION_REQUIRED"
    TRADING_BLOCKED = "TRADING_BLOCKED"
    MARKET_CLOSED = "MARKET_CLOSED"


@dataclass
class HealthResult:
    service: ServiceName
    status: HealthStatus
    liveness: HealthStatus = HealthStatus.UNKNOWN
    readiness: HealthStatus = HealthStatus.UNKNOWN
    latency_ms: float | None = None
    detail: str = ""
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # structured diagnostics — all optional, only populated when known
    reason: str | None = None  # machine: tcp_refused, login_marker_missing, http_failed, pid_not_found, etc.
    host: str | None = None
    port: int | None = None
    pid: int | None = None
    exit_code: int | None = None
    signal: str | None = None
    endpoint: str | None = None  # e.g. /health/ready
    endpoint_url: str | None = None
    dependency: str | None = None  # e.g. postgres, redis, tws
    underlying_error: str | None = None  # sanitized exception string
    log_marker: str | None = None  # expected marker e.g. Login has completed
    log_excerpt: str | None = None  # bounded sanitized excerpt (max 500 chars)
    # human-readable sections (formatter may derive if not set)
    what_happened: str | None = None
    impact: str | None = None
    trading_impact: str | None = None
    recovery_action: str | None = None
    operator_action: str | None = None


@dataclass
class ServiceSnapshot:
    service: ServiceName
    state: ServiceState = ServiceState.UNKNOWN
    last_health: HealthResult | None = None
    last_transition_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    recovery_attempts: list[datetime] = field(default_factory=list)
    last_recovery_at: datetime | None = None
    recovery_failed_count: int = 0
    failure_reason: str = ""


@dataclass
class SafetyGateResult:
    passed: bool
    failures: list[str] = field(default_factory=list)
    details: str = ""
    gates: dict[str, str] = field(default_factory=dict)  # gate -> SAFE/UNSAFE/UNKNOWN


# Event -> emoji mapping for Telegram
EVENT_EMOJI: dict[NotificationEvent, str] = {
    NotificationEvent.START: "🟢",
    NotificationEvent.STOP: "⚪",
    NotificationEvent.FAILURE: "🔴",
    NotificationEvent.UNHEALTHY: "🟡",
    NotificationEvent.RECOVERY_STARTED: "🔄",
    NotificationEvent.RECOVERED: "✅",
    NotificationEvent.RECOVERY_FAILED: "❌",
    NotificationEvent.MANUAL_INTERVENTION_REQUIRED: "🚨",
    NotificationEvent.TRADING_BLOCKED: "⛔",
    NotificationEvent.MARKET_CLOSED: "🟡",
}
