"""Service for collecting read-only system metrics and service health states."""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import socket
import time
from datetime import UTC, datetime
from datetime import time as dtime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import psutil
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.schemas.system_monitor import (
    AlertItem,
    CpuMetrics,
    MemoryMetrics,
    MetricUsage,
    ProcessInfo,
    ServicesHealth,
    ServiceStatus,
    StorageMetrics,
    SystemInfoResponse,
    SystemMonitorResponse,
)


def _is_trading_session(now: datetime | None = None) -> bool:
    tz = ZoneInfo("America/New_York")
    now_et = (now or datetime.now(tz)).astimezone(tz)
    if now_et.weekday() >= 5:
        return False
    return dtime(9, 30) <= now_et.time() < dtime(16, 0)

logger = logging.getLogger(__name__)


async def collect_system_monitor_data(
    session: AsyncSession | None = None,
    tws_client: Any | None = None,
    redis_client: Redis | None = None,
    account_margin: Any | None = None,
) -> SystemMonitorResponse:
    """Collect complete system metrics and service health states safely without side-effects."""
    settings = get_settings()
    gw_host = settings.ibkr_host
    gw_port = settings.ibkr_port

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

    # 5. Service Health Checks (market-aware for trading-hours services)
    is_open = _is_trading_session(now)
    backend_status = await _check_backend_health()
    demo_stream_status = await _check_demo_stream_health()
    ib_gateway_status = await _check_ib_gateway_health(tws_client, gw_host=gw_host, gw_port=gw_port)
    # Outside session, Gateway STOPPED is expected MARKET_CLOSED, not failure
    if not is_open and ib_gateway_status.status == "STOPPED":
        ib_gateway_status = ServiceStatus(
            name=ib_gateway_status.name,
            status="MARKET_CLOSED",
            port=ib_gateway_status.port,
            health_detail="Expected outside trading session (09:30-16:00 ET)",
            latency_ms=None,
        )
    webhook_status = await _check_webhook_health()
    if not is_open and webhook_status.status == "STOPPED":
        webhook_status = ServiceStatus(
            name=webhook_status.name,
            status="MARKET_CLOSED",
            port=webhook_status.port,
            health_detail="Expected outside trading session (09:30-16:00 ET)",
            latency_ms=None,
        )
    watchdog_status = await _check_watchdog_health()
    postgres_status = await _check_postgresql_health(session)
    redis_status = await _check_redis_health(redis_client)

    services_health = ServicesHealth(
        backend=backend_status,
        demo_stream=demo_stream_status,
        ib_gateway=ib_gateway_status,
        webhook=webhook_status,
        watchdog=watchdog_status,
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
        "open_ports": [8000, 8001, 8010, 5432, 6379, gw_port],
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

    # Service Alerts — market-aware: MARKET_CLOSED is expected, not a failure
    all_services = [
        ("Demo Streaming", demo_stream_status),
        ("IB Gateway", ib_gateway_status),
        ("Webhook Ingest", webhook_status),
        ("Watchdog", watchdog_status),
        ("PostgreSQL", postgres_status),
        ("Redis", redis_status),
        ("Trading Backend", backend_status),
    ]

    for svc_name, svc in all_services:
        if svc.status == "MARKET_CLOSED":
            # Expected outside trading session (Gateway/Webhook) — not an alert
            continue
        if svc.status in ("STOPPED", "UNKNOWN"):
            alerts.append(AlertItem(level="CRITICAL", component=svc_name, message=f"Service {svc_name} is unavailable ({svc.status})"))
            overall_status = "CRITICAL"
        elif svc.status == "DEGRADED":
            alerts.append(AlertItem(level="WARNING", component=svc_name, message=f"Service {svc_name} is degraded ({svc.health_detail})"))
            if overall_status != "CRITICAL":
                overall_status = "DEGRADED"

    # Market-closed overall: if no real failure and market is closed, overall is MARKET_CLOSED (not HEALTHY)
    if not is_open and overall_status == "HEALTHY":
        if ib_gateway_status.status == "MARKET_CLOSED" or webhook_status.status == "MARKET_CLOSED":
            overall_status = "MARKET_CLOSED"

    if account_margin is not None:
        try:
            snapshots = account_margin.all_snapshots()
            if not snapshots:
                alerts.append(
                    AlertItem(
                        level="WARNING",
                        component="Margin",
                        message="No live IBKR account-margin snapshot yet",
                    )
                )
                if overall_status == "HEALTHY":
                    overall_status = "DEGRADED"
            for snap in snapshots.values():
                if snap.is_stale:
                    alerts.append(
                        AlertItem(
                            level="CRITICAL",
                            component="Margin",
                            message=f"{snap.ibkr_account} margin snapshot is stale",
                        )
                    )
                    overall_status = "CRITICAL"
                elif snap.available_funds is not None and snap.available_funds <= 0:
                    alerts.append(
                        AlertItem(
                            level="CRITICAL",
                            component="Margin",
                            message=f"{snap.ibkr_account} AvailableFunds={snap.available_funds}",
                        )
                    )
                    overall_status = "CRITICAL"
        except Exception:
            logger.exception("Failed to collect account margin for system monitor")

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


async def _check_ib_gateway_health(
    tws_client: Any | None = None,
    gw_host: str | None = None,
    gw_port: int | None = None,
) -> ServiceStatus:
    start_t = time.perf_counter()
    if gw_host is None or gw_port is None:
        settings = get_settings()
        gw_host = settings.ibkr_host
        gw_port = settings.ibkr_port

    # Check if TWSClient object reports active connection
    if tws_client is not None and getattr(tws_client, "is_connected", lambda: False)():
        elapsed_ms = round((time.perf_counter() - start_t) * 1000, 1)
        client_id = getattr(tws_client, "client_id", 1)
        return ServiceStatus(
            name="IB Gateway",
            status="RUNNING",
            port=gw_port,
            health_detail=f"Connected to IBKR Paper socket (ClientID={client_id})",
            latency_ms=elapsed_ms,
        )

    # Fallback to direct TCP socket check on configured gw_host:gw_port
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(gw_host, gw_port),
            timeout=1.5,
        )
        elapsed_ms = round((time.perf_counter() - start_t) * 1000, 1)
        writer.close()
        await writer.wait_closed()
        return ServiceStatus(
            name="IB Gateway",
            status="RUNNING",
            port=gw_port,
            health_detail=f"Listening on socket port {gw_port}",
            latency_ms=elapsed_ms,
        )
    except Exception as exc:
        return ServiceStatus(
            name="IB Gateway",
            status="STOPPED",
            port=gw_port,
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


async def _check_backend_health() -> ServiceStatus:
    start_t = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            res = await client.get("http://127.0.0.1:8001/health")
            elapsed_ms = round((time.perf_counter() - start_t) * 1000, 1)
            if res.status_code == 200:
                return ServiceStatus(
                    name="FastAPI Backend",
                    status="RUNNING",
                    port=8001,
                    health_detail="Trading engine API responsive",
                    latency_ms=elapsed_ms,
                )
            return ServiceStatus(
                name="FastAPI Backend",
                status="DEGRADED",
                port=8001,
                health_detail=f"HTTP {res.status_code}",
                latency_ms=elapsed_ms,
            )
    except Exception as exc:
        return ServiceStatus(
            name="FastAPI Backend",
            status="STOPPED",
            port=8001,
            health_detail=f"Unreachable: {exc}",
            latency_ms=None,
        )


async def _check_webhook_health() -> ServiceStatus:
    start_t = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            res = await client.get("http://127.0.0.1:8000/health")
            elapsed_ms = round((time.perf_counter() - start_t) * 1000, 1)
            if res.status_code == 200:
                return ServiceStatus(
                    name="Webhook Ingest",
                    status="RUNNING",
                    port=8000,
                    health_detail="Webhook ingest responsive",
                    latency_ms=elapsed_ms,
                )
            return ServiceStatus(
                name="Webhook Ingest",
                status="DEGRADED",
                port=8000,
                health_detail=f"HTTP {res.status_code}",
                latency_ms=elapsed_ms,
            )
    except Exception as exc:
        return ServiceStatus(
            name="Webhook Ingest",
            status="STOPPED",
            port=8000,
            health_detail=f"Unreachable: {exc}",
            latency_ms=None,
        )


async def _check_watchdog_health() -> ServiceStatus:
    start_t = time.perf_counter()
    try:
        # Check if watchdog process is running via psutil
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmd = " ".join(p.info.get("cmdline") or [])
                if "watchdog" in cmd and "watchdog_main" in cmd:
                    elapsed_ms = round((time.perf_counter() - start_t) * 1000, 1)
                    return ServiceStatus(
                        name="Watchdog",
                        status="RUNNING",
                        port=0,
                        health_detail=f"Watchdog observer running (PID {p.info['pid']})",
                        latency_ms=elapsed_ms,
                    )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return ServiceStatus(
            name="Watchdog",
            status="STOPPED",
            port=0,
            health_detail="Watchdog process not found",
            latency_ms=None,
        )
    except Exception as exc:
        return ServiceStatus(
            name="Watchdog",
            status="UNKNOWN",
            port=0,
            health_detail=f"Check failed: {exc}",
            latency_ms=None,
        )
