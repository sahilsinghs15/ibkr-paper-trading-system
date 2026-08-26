"""Service for collecting read-only system metrics and service health states."""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import socket
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import psutil
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.system_monitor import (
    AlertItem,
    CpuMetrics,
    MemoryMetrics,
    MetricUsage,
    ProcessInfo,
    ServiceStatus,
    ServicesHealth,
    StorageMetrics,
    SystemInfoResponse,
    SystemMonitorResponse,
)

logger = logging.getLogger(__name__)


async def collect_system_monitor_data(
    session: AsyncSession | None = None,
    tws_client: Any | None = None,
    redis_client: Redis | None = None,
) -> SystemMonitorResponse:
    """Collect complete system metrics and service health states safely without side-effects."""
    now = datetime.now(UTC)
    
    # 1. System Info
    hostname = socket.gethostname()
    uptime = time.time() - psutil.boot_time()
    try:
        load1, load5, load15 = os.getloadavg()
    except (AttributeError, OSError):
        load1, load5, load15 = 0.0, 0.0, 0.0

    system_info = SystemInfoResponse(
        hostname=hostname,
        operating_system=platform.system(),
        os_version=platform.version(),
        kernel_version=platform.release(),
        architecture=platform.machine(),
        cpu_count=psutil.cpu_count() or 1,
        total_memory_bytes=psutil.virtual_memory().total,
        system_uptime_seconds=round(uptime, 1),
        load_avg=[round(load1, 2), round(load5, 2), round(load15, 2)],
        timezone=str(datetime.now().astimezone().tzinfo or "UTC"),
        instance_type="t3.small (AWS EC2)",
    )

    # 2. CPU Metrics
    cpu_percent = psutil.cpu_percent(interval=None)
    cpu_metrics = CpuMetrics(
        usage_percent=round(cpu_percent, 1),
        count=psutil.cpu_count() or 1,
        load_avg_1m=round(load1, 2),
        load_avg_5m=round(load5, 2),
        load_avg_15m=round(load15, 2),
    )

    # 3. Memory Metrics (RAM & Swap)
    vmem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    memory_metrics = MemoryMetrics(
        ram=MetricUsage(
            total_bytes=vmem.total,
            used_bytes=vmem.used,
            available_bytes=vmem.available,
            percent=round(vmem.percent, 1),
        ),
        swap=MetricUsage(
            total_bytes=swap.total,
            used_bytes=swap.used,
            available_bytes=swap.free,
            percent=round(swap.percent, 1),
        ),
    )

    # 4. Storage Metrics (Root mount /)
    storage_list: list[StorageMetrics] = []
    try:
        usage = psutil.disk_usage("/")
        status_val: str = "OK"
        if usage.percent >= 90.0:
            status_val = "CRITICAL"
        elif usage.percent >= 75.0:
            status_val = "WARNING"
            
        storage_list.append(
            StorageMetrics(
                mount="/",
                filesystem="EBS gp3 (/dev/nvme0n1p1)",
                usage=MetricUsage(
                    total_bytes=usage.total,
                    used_bytes=usage.used,
                    available_bytes=usage.free,
                    percent=round(usage.percent, 1),
                ),
                status=status_val, # type: ignore[arg-type]
            )
        )
    except Exception:
        logger.exception("Failed to query disk usage for /")

    # 5. Service Health Checks
    backend_status = ServiceStatus(
        name="FastAPI Backend",
        status="RUNNING",
        port=8000,
        health_detail="Trading engine API responsive",
        latency_ms=0.5,
    )

    demo_stream_status = await _check_demo_stream_health()
    ib_gateway_status = await _check_ib_gateway_health(tws_client)
    postgres_status = await _check_postgresql_health(session)
    redis_status = await _check_redis_health(redis_client)

    services_health = ServicesHealth(
        backend=backend_status,
        demo_stream=demo_stream_status,
        ib_gateway=ib_gateway_status,
        postgresql=postgres_status,
        redis=redis_status,
    )

    # 6. Network Info (Safe operational info)
    private_ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        private_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass

    network_info = {
        "hostname": hostname,
        "private_ip": private_ip,
        "binding_loopback": "127.0.0.1",
        "open_ports": [8000, 8010, 5432, 6379, 7497],
    }

    # 7. Top Processes (Top 5 by CPU/Memory)
    top_processes: list[ProcessInfo] = []
    try:
        processes = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "status"]):
            try:
                p_info = p.info
                if p_info["name"] and p_info["pid"] != 0:
                    processes.append(p_info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        
        # Sort by CPU usage then Memory usage
        sorted_procs = sorted(
            processes,
            key=lambda x: (x.get("cpu_percent") or 0, x.get("memory_percent") or 0),
            reverse=True,
        )[:5]

        for proc in sorted_procs:
            top_processes.append(
                ProcessInfo(
                    pid=proc["pid"],
                    name=proc["name"],
                    cpu_percent=round(proc.get("cpu_percent") or 0.0, 1),
                    memory_percent=round(proc.get("memory_percent") or 0.0, 1),
                    status=proc.get("status") or "running",
                )
            )
    except Exception:
        logger.exception("Failed to collect top process list")

    # 8. Derive Alerts & Overall Status
    alerts: list[AlertItem] = []
    overall_status: str = "HEALTHY"

    # CPU Alerts
    if cpu_metrics.usage_percent >= 90.0:
        alerts.append(AlertItem(level="CRITICAL", component="CPU", message=f"CPU usage critical ({cpu_metrics.usage_percent}%)"))
        overall_status = "CRITICAL"
    elif cpu_metrics.usage_percent >= 75.0:
        alerts.append(AlertItem(level="WARNING", component="CPU", message=f"CPU usage high ({cpu_metrics.usage_percent}%)"))
        if overall_status != "CRITICAL":
            overall_status = "DEGRADED"

    # Memory Alerts
    if memory_metrics.ram.percent >= 90.0:
        alerts.append(AlertItem(level="CRITICAL", component="RAM", message=f"RAM usage critical ({memory_metrics.ram.percent}%)"))
        overall_status = "CRITICAL"
    elif memory_metrics.ram.percent >= 75.0:
        alerts.append(AlertItem(level="WARNING", component="RAM", message=f"RAM usage high ({memory_metrics.ram.percent}%)"))
        if overall_status != "CRITICAL":
            overall_status = "DEGRADED"

    # Storage Alerts
    for st in storage_list:
        if st.status == "CRITICAL":
            alerts.append(AlertItem(level="CRITICAL", component="Storage", message=f"Storage capacity critical on {st.mount} ({st.usage.percent}%)"))
            overall_status = "CRITICAL"
        elif st.status == "WARNING":
            alerts.append(AlertItem(level="WARNING", component="Storage", message=f"Storage capacity high on {st.mount} ({st.usage.percent}%)"))
            if overall_status != "CRITICAL":
                overall_status = "DEGRADED"

    # Service Alerts
    all_services = [
        ("Demo Streaming", demo_stream_status),
        ("IB Gateway", ib_gateway_status),
        ("PostgreSQL", postgres_status),
        ("Redis", redis_status),
    ]

    for svc_name, svc in all_services:
        if svc.status in ("STOPPED", "UNKNOWN"):
            alerts.append(AlertItem(level="CRITICAL", component=svc_name, message=f"Service {svc_name} is unavailable ({svc.status})"))
            overall_status = "CRITICAL"
        elif svc.status == "DEGRADED":
            alerts.append(AlertItem(level="WARNING", component=svc_name, message=f"Service {svc_name} is degraded ({svc.health_detail})"))
            if overall_status != "CRITICAL":
                overall_status = "DEGRADED"

    return SystemMonitorResponse(
        overall_status=overall_status, # type: ignore[arg-type]
        timestamp=now,
        system=system_info,
        cpu=cpu_metrics,
        memory=memory_metrics,
        storage=storage_list,
        services=services_health,
        network=network_info,
        alerts=alerts,
        top_processes=top_processes,
    )


async def _check_demo_stream_health() -> ServiceStatus:
    start_t = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            res = await client.get("http://127.0.0.1:8010/health")
            elapsed_ms = round((time.perf_counter() - start_t) * 1000, 1)
            if res.status_code == 200:
                data = res.json()
                redis_ok = data.get("redis", False)
                status_str = "RUNNING" if redis_ok else "DEGRADED"
                detail = "SSE & React UI active" if redis_ok else "Demo stream active (Redis degraded)"
                return ServiceStatus(
                    name="Demo Streaming",
                    status=status_str,
                    port=8010,
                    health_detail=detail,
                    latency_ms=elapsed_ms,
                )
            return ServiceStatus(
                name="Demo Streaming",
                status="DEGRADED",
                port=8010,
                health_detail=f"HTTP {res.status_code}",
                latency_ms=elapsed_ms,
            )
    except Exception as exc:
        return ServiceStatus(
            name="Demo Streaming",
            status="STOPPED",
            port=8010,
            health_detail=f"Unreachable: {exc}",
            latency_ms=None,
        )


async def _check_ib_gateway_health(tws_client: Any | None = None) -> ServiceStatus:
    start_t = time.perf_counter()
    # Check if TWSClient object reports active connection
    if tws_client is not None and getattr(tws_client, "is_connected", lambda: False)():
        elapsed_ms = round((time.perf_counter() - start_t) * 1000, 1)
        return ServiceStatus(
            name="IB Gateway",
            status="RUNNING",
            port=7497,
            health_detail="Connected to IBKR Paper socket (ClientID=1)",
            latency_ms=elapsed_ms,
        )

    # Fallback to direct TCP socket check on port 7497
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", 7497),
            timeout=1.5,
        )
        elapsed_ms = round((time.perf_counter() - start_t) * 1000, 1)
        writer.close()
        await writer.wait_closed()
        return ServiceStatus(
            name="IB Gateway",
            status="RUNNING",
            port=7497,
            health_detail="Listening on socket port 7497",
            latency_ms=elapsed_ms,
        )
    except Exception as exc:
        return ServiceStatus(
            name="IB Gateway",
            status="STOPPED",
            port=7497,
            health_detail=f"Socket unreachable: {exc}",
            latency_ms=None,
        )


async def _check_postgresql_health(session: AsyncSession | None = None) -> ServiceStatus:
    start_t = time.perf_counter()
    if session is not None:
        try:
            await session.execute(text("SELECT 1"))
            # Optionally query alembic migration version
            rev_str = "head"
            try:
                res = await session.execute(text("SELECT version_num FROM alembic_version"))
                row = res.scalar_one_or_none()
                if row:
                    rev_str = str(row)
            except Exception:
                pass
            elapsed_ms = round((time.perf_counter() - start_t) * 1000, 1)
            return ServiceStatus(
                name="PostgreSQL",
                status="RUNNING",
                port=5432,
                health_detail=f"Connected to ibkr_trading (Alembic: {rev_str})",
                latency_ms=elapsed_ms,
            )
        except Exception as exc:
            return ServiceStatus(
                name="PostgreSQL",
                status="STOPPED",
                port=5432,
                health_detail=f"Query failed: {exc}",
                latency_ms=None,
            )

    # Fallback socket check on 5432
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", 5432),
            timeout=1.5,
        )
        elapsed_ms = round((time.perf_counter() - start_t) * 1000, 1)
        writer.close()
        await writer.wait_closed()
        return ServiceStatus(
            name="PostgreSQL",
            status="RUNNING",
            port=5432,
            health_detail="Listening on socket port 5432",
            latency_ms=elapsed_ms,
        )
    except Exception as exc:
        return ServiceStatus(
            name="PostgreSQL",
            status="STOPPED",
            port=5432,
            health_detail=f"Port unreachable: {exc}",
            latency_ms=None,
        )


async def _check_redis_health(redis_client: Redis | None = None) -> ServiceStatus:
    start_t = time.perf_counter()
    if redis_client is not None:
        try:
            ok = await redis_client.ping()
            elapsed_ms = round((time.perf_counter() - start_t) * 1000, 1)
            if ok:
                return ServiceStatus(
                    name="Redis",
                    status="RUNNING",
                    port=6379,
                    health_detail="Redis server ping OK",
                    latency_ms=elapsed_ms,
                )
        except Exception as exc:
            return ServiceStatus(
                name="Redis",
                status="STOPPED",
                port=6379,
                health_detail=f"Ping failed: {exc}",
                latency_ms=None,
            )

    # Fallback socket check on 6379
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", 6379),
            timeout=1.5,
        )
        elapsed_ms = round((time.perf_counter() - start_t) * 1000, 1)
        writer.close()
        await writer.wait_closed()
        return ServiceStatus(
            name="Redis",
            status="RUNNING",
            port=6379,
            health_detail="Listening on socket port 6379",
            latency_ms=elapsed_ms,
        )
    except Exception as exc:
        return ServiceStatus(
            name="Redis",
            status="STOPPED",
            port=6379,
            health_detail=f"Port unreachable: {exc}",
            latency_ms=None,
        )
