"""Pydantic schemas for the System Monitor API."""

from __future__ import annotations

from datetime import date as _date
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class MetricUsage(BaseModel):
    """Generic utilization schema for memory, swap, disk."""
    total_bytes: int = Field(..., description="Total capacity in bytes")
    used_bytes: int = Field(..., description="Used capacity in bytes")
    available_bytes: int = Field(..., description="Available capacity in bytes")
    percent: float = Field(..., description="Utilization percentage (0-100)")


class CpuMetrics(BaseModel):
    """CPU metrics schema."""
    usage_percent: float = Field(..., description="Current total CPU utilization percentage")
    count: int = Field(..., description="Total CPU logical core count")
    load_avg_1m: float = Field(..., description="System 1-minute load average")
    load_avg_5m: float = Field(..., description="System 5-minute load average")
    load_avg_15m: float = Field(..., description="System 15-minute load average")


class MemoryMetrics(BaseModel):
    """RAM and Swap metrics schema."""
    ram: MetricUsage
    swap: MetricUsage


class StorageMetrics(BaseModel):
    """Filesystem storage metrics schema."""
    mount: str = Field(..., description="Filesystem mount point")
    filesystem: str = Field(..., description="Device or filesystem name")
    usage: MetricUsage
    status: Literal["OK", "WARNING", "CRITICAL"] = Field(..., description="Storage health status based on thresholds")


class ServiceStatus(BaseModel):
    """Health status of an individual system dependency or application service."""
    name: str = Field(..., description="Human-readable service name")
    status: Literal["RUNNING", "DEGRADED", "STOPPED", "UNKNOWN", "MARKET_CLOSED"] = Field(..., description="Operational status")
    port: int = Field(..., description="Binding port number")
    health_detail: str = Field(..., description="Descriptive status or version detail")
    latency_ms: float | None = Field(None, description="Response latency in milliseconds if applicable")


class ServicesHealth(BaseModel):
    """Overall applications and infrastructure services health."""
    backend: ServiceStatus
    demo_stream: ServiceStatus
    ib_gateway: ServiceStatus
    webhook: ServiceStatus
    watchdog: ServiceStatus
    postgresql: ServiceStatus
    redis: ServiceStatus


class ProcessInfo(BaseModel):
    """Information on a top resource-consuming process."""
    pid: int
    name: str
    cpu_percent: float
    memory_percent: float
    status: str


class AlertItem(BaseModel):
    """System alert item."""
    level: Literal["INFO", "WARNING", "CRITICAL"]
    component: str
    message: str


class SystemInfoResponse(BaseModel):
    """System platform information."""
    hostname: str
    operating_system: str
    os_version: str
    kernel_version: str
    architecture: str
    cpu_count: int
    total_memory_bytes: int
    system_uptime_seconds: float
    load_avg: list[float]
    timezone: str
    instance_type: str = "t3.small (AWS EC2)"


class DailyCreditUsage(BaseModel):
    """Daily instance cost (ESTIMATE for current day, ACTUAL for completed)."""

    date: _date | None = Field(None, description="Usage date (UTC)")
    amount_usd: Decimal | None = Field(None, description="Total daily cost (EC2 + Public IPv4)")
    ec2_cost_usd: Decimal | None = Field(None, description="EC2 component")
    public_ipv4_cost_usd: Decimal | None = Field(None, description="Public IPv4 component")
    status: Literal["ACTUAL", "ESTIMATE", "UNAVAILABLE", "STALE", "FAILED"] = Field(
        ..., description="ACTUAL=Cost Explorer, ESTIMATE=latest actual carried forward"
    )
    source: str | None = Field(None, description="CE source API")
    source_date: _date | None = Field(None, description="Estimate source date (latest actual)")
    fetched_at: datetime | None = Field(None, description="Last successful AWS fetch")


class MonthlyCreditUsage(BaseModel):
    """Monthly aggregation derived from persisted daily ledger + current-day estimate."""

    month_start: _date = Field(..., description="First day of current month (inclusive)")
    month_end: _date = Field(..., description="Current date (inclusive)")
    actual_through: _date | None = Field(None, description="Latest ACTUAL date included")
    actual_total_usd: Decimal = Field(..., description="SUM(actual completed-day totals)")
    current_estimate_usd: Decimal | None = Field(None, description="Today ESTIMATE (latest actual)")
    estimate_date: _date | None = Field(None, description="Date for which estimate applies (today)")
    estimate_source_date: _date | None = Field(None, description="Actual date used as estimate basis")
    displayed_total_usd: Decimal | None = Field(None, description="actual_total + estimate (shown in UI)")
    is_stale: bool = Field(False, description="True if latest actual older than threshold")
    fetched_at: datetime | None = Field(None, description="Last successful AWS fetch timestamp")
    status: Literal["OK", "STALE", "UNAVAILABLE"] = Field(..., description="Ledger freshness")


class CreditUsageResponse(BaseModel):
    """Credit usage container for System Monitor."""

    daily: DailyCreditUsage
    monthly: MonthlyCreditUsage
    instance_id: str | None = Field(None, description="EC2 instance identity (if discovered)")
    region: str | None = Field(None, description="AWS region")


class SystemMonitorResponse(BaseModel):
    """Root response model for GET /api/v1/system-monitor."""
    overall_status: Literal["HEALTHY", "DEGRADED", "CRITICAL", "MARKET_CLOSED"]
    timestamp: datetime
    system: SystemInfoResponse
    cpu: CpuMetrics
    memory: MemoryMetrics
    storage: list[StorageMetrics]
    services: ServicesHealth
    network: dict[str, Any]
    alerts: list[AlertItem]
    top_processes: list[ProcessInfo]
    credit: CreditUsageResponse | None = Field(None, description="Instance credit usage (EC2 + Public IPv4)")

    model_config = ConfigDict(frozen=True)
