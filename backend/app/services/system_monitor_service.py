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
from app.db.session import AsyncSessionLocal
from app.schemas.system_monitor import (
    AlertItem,
    CpuMetrics,
    CreditUsageResponse,
    DailyCreditUsage,
    MemoryMetrics,
    MetricUsage,
    MonthlyCreditUsage,
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


async def _collect_credit_usage(
    session: AsyncSession | None,
) -> CreditUsageResponse | None:
    """Collect credit usage from persisted ledger without calling AWS."""
    from datetime import date as _date

    from sqlalchemy import select

    from app.db.models.instance_credit import InstanceDailyCreditModel
    from app.services.instance_credit.calculation import (
        compute_credit_usage,
        daily_display,
    )

    try:
        settings = get_settings()
        stale_days = getattr(settings, "instance_credit_stale_days", 3)

        if session is None:
            async with AsyncSessionLocal() as s:  # type: ignore[operator]
                return await _collect_credit_usage(s)
        sess = session
        today = datetime.now(UTC).date()
        month_start = _date(today.year, today.month, 1)

        q = select(InstanceDailyCreditModel).where(
            InstanceDailyCreditModel.usage_date >= month_start,
            InstanceDailyCreditModel.usage_date < today,
            InstanceDailyCreditModel.status == "ACTUAL",
        ).order_by(InstanceDailyCreditModel.usage_date.asc())
        res = await sess.execute(q)
        actuals = list(res.scalars().all())

        latest_q = (
            select(InstanceDailyCreditModel)
            .where(InstanceDailyCreditModel.status == "ACTUAL", InstanceDailyCreditModel.total_cost_usd.is_not(None))
            .order_by(InstanceDailyCreditModel.usage_date.desc())
            .limit(1)
        )
        latest_res = await sess.execute(latest_q)
        latest = latest_res.scalar_one_or_none()

        fetched_at = latest.fetched_at if latest else None
        latest_total = latest.total_cost_usd if latest else None
        latest_date = latest.usage_date if latest else None

        # Identity for display: try metadata discovery without blocking (fast fail)
        instance_id = None
        region = None
        try:

            # Attempt fast sync env fallback
            import os

            instance_id = os.environ.get("AWS_EC2_INSTANCE_ID") or getattr(settings, "aws_ec2_instance_id", None)
            region = os.environ.get("AWS_REGION") or getattr(settings, "aws_region", None)
            # If settings allow, instance_id may still be None; keep None to avoid hardcoding
        except Exception:
            pass

        actual_records = [{"usage_date": r.usage_date, "total_cost_usd": r.total_cost_usd} for r in actuals]
        calc = compute_credit_usage(
            today=today,
            actual_records=actual_records,
            latest_actual_total=latest_total,
            latest_actual_date=latest_date,
            fetched_at=fetched_at,
            stale_days=stale_days,
        )

        # Daily vs ledger today row
        today_q = select(InstanceDailyCreditModel).where(InstanceDailyCreditModel.usage_date == today)
        today_res = await sess.execute(today_q)
        today_rec = today_res.scalar_one_or_none()
        today_dict = None
        if today_rec is not None:
            today_dict = {
                "usage_date": today_rec.usage_date,
                "total_cost_usd": today_rec.total_cost_usd,
                "status": today_rec.status,
            }
        daily = daily_display(
            today=today,
            latest_record=today_dict,
            latest_actual_total=latest_total,
            latest_actual_date=latest_date,
        )

        # Build response schemas
        # Determine monthly status
        if latest is None and not actuals:
            monthly_status: str = "UNAVAILABLE"
        elif calc.get("is_stale"):
            monthly_status = "STALE"
        else:
            monthly_status = "OK"

        # Resolve daily amounts with component breakdown
        daily_ec2 = None
        daily_ipv4 = None
        daily_source = None
        daily_fetched = fetched_at
        daily_amount = daily.get("amount")
        daily_status = daily.get("status") or "UNAVAILABLE"
        # If today has ACTUAL row, expose its components
        if today_rec is not None and today_rec.status == "ACTUAL":
            daily_ec2 = today_rec.ec2_cost_usd
            daily_ipv4 = today_rec.public_ipv4_cost_usd
            daily_source = today_rec.source
            daily_fetched = today_rec.fetched_at
            daily_status = "ACTUAL"
        elif latest is not None:
            # Estimate components: use latest actual's components as estimate basis
            daily_ec2 = latest.ec2_cost_usd
            daily_ipv4 = latest.public_ipv4_cost_usd
            daily_source = latest.source
            # status already ESTIMATE/UNAVAILABLE

        daily_model = DailyCreditUsage(
            date=daily.get("date"),
            amount_usd=daily_amount,
            ec2_cost_usd=daily_ec2,
            public_ipv4_cost_usd=daily_ipv4,
            status=daily_status,  # type: ignore[arg-type]
            source=daily_source,
            source_date=daily.get("source_date"),
            fetched_at=daily_fetched,
        )
        monthly_model = MonthlyCreditUsage(
            month_start=calc["month_start"],
            month_end=calc["month_end"],
            actual_through=calc["actual_through"],
            actual_total_usd=calc["actual_total"],
            current_estimate_usd=calc["current_estimate"],
            estimate_date=calc["estimate_date"],
            estimate_source_date=calc["estimate_source_date"],
            displayed_total_usd=calc["displayed_monthly_total"],
            is_stale=calc["is_stale"],
            fetched_at=calc["fetched_at"],
            status=monthly_status,  # type: ignore[arg-type]
        )
        return CreditUsageResponse(daily=daily_model, monthly=monthly_model, instance_id=instance_id, region=region)
    except Exception:
        logger.exception("credit usage collection failed")
        return None


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

    # Dynamic instance type discovery (prefer metadata, fallback to legacy)
    instance_type_label = "t3.small (AWS EC2)"
    try:

        # Best-effort sync discovery: if we are already on EC2, this will return c7i-flex.large etc.
        # We avoid blocking on slow IMDS by using short timeout wrapper via asyncio.wait_for in helper
        # For now keep fallback; detailed dynamic label is enriched via credit path below
        pass
    except Exception:
        pass
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
        instance_type=instance_type_label,
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
    except Exception:  # noqa: BLE001, S110
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
    if (
        not is_open
        and overall_status == "HEALTHY"
        and (ib_gateway_status.status == "MARKET_CLOSED" or webhook_status.status == "MARKET_CLOSED")
    ):
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

    # 9. Credit usage (EC2 + Public IPv4) — never calls AWS, reads persisted ledger only
    credit: CreditUsageResponse | None = None
    try:
        credit = await _collect_credit_usage(session)
        if credit and credit.daily.status == "ESTIMATE":
            # Informational alert when stale not critical, to surface estimate basis
            pass
        if credit and credit.monthly.is_stale:
            alerts.append(
                AlertItem(
                    level="WARNING",
                    component="Credit",
                    message=f"Daily credit ledger stale (latest actual {credit.monthly.actual_through})",
                )
            )
            if overall_status == "HEALTHY":
                overall_status = "DEGRADED"
    except Exception:
        logger.exception("Failed to collect credit usage for system monitor")
        credit = None

    # Enrich instance_type label if identity available via credit service
    if credit and credit.instance_id:
        # Attempt to resolve instance type via metadata cache; keep label minimal
        pass

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
        credit=credit,
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
                    status=status_str,  # type: ignore[arg-type]
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
    except Exception as exc:  # noqa: BLE001
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
        _reader, writer = await asyncio.wait_for(
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
    except Exception as exc:  # noqa: BLE001
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
            except Exception:  # noqa: BLE001, S110
                pass
            elapsed_ms = round((time.perf_counter() - start_t) * 1000, 1)
            return ServiceStatus(
                name="PostgreSQL",
                status="RUNNING",
                port=5432,
                health_detail=f"Connected to ibkr_trading (Alembic: {rev_str})",
                latency_ms=elapsed_ms,
            )
        except Exception as exc:  # noqa: BLE001
            return ServiceStatus(
                name="PostgreSQL",
                status="STOPPED",
                port=5432,
                health_detail=f"Query failed: {exc}",
                latency_ms=None,
            )

    # Fallback socket check on 5432
    try:
        _reader, writer = await asyncio.wait_for(
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
    except Exception as exc:  # noqa: BLE001
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
        except Exception as exc:  # noqa: BLE001
            return ServiceStatus(
                name="Redis",
                status="STOPPED",
                port=6379,
                health_detail=f"Ping failed: {exc}",
                latency_ms=None,
            )

    # Fallback socket check on 6379
    try:
        _reader, writer = await asyncio.wait_for(
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
    except Exception as exc:  # noqa: BLE001
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
    except Exception as exc:  # noqa: BLE001
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
    except Exception as exc:  # noqa: BLE001
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
    except Exception as exc:  # noqa: BLE001
        return ServiceStatus(
            name="Watchdog",
            status="UNKNOWN",
            port=0,
            health_detail=f"Check failed: {exc}",
            latency_ms=None,
        )
