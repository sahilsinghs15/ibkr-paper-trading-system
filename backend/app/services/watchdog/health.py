"""Health checkers — liveness vs readiness with structured diagnostics.

Each checker is lightweight, timeout-bounded, and never assumes root cause beyond evidence.
Watchdog failure must never cascade to trading.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import time
from pathlib import Path

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.services.watchdog.config import WatchdogSettings
from app.services.watchdog.models import HealthResult, HealthStatus, ServiceName

logger = logging.getLogger(__name__)


class ServiceHealthChecker:
    async def check(self) -> HealthResult:  # pragma: no cover - interface
        raise NotImplementedError


def _tcp_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """Sync TCP open used only in fallback paths; prefer _tcp_open_async in async checks."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def _tcp_open_async(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def _http_get(url: str, timeout: float = 2.0) -> tuple[bool, str, float | None]:
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            elapsed = round((time.perf_counter() - start) * 1000, 1)
            if 200 <= resp.status_code < 300:
                return True, f"HTTP {resp.status_code}", elapsed
            return False, f"HTTP {resp.status_code}", elapsed
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}", None


# ---- sanitization / helpers ----

_SENSITIVE_RE = re.compile(r"(bot_token|password|secret|chat_id|api_key).{0,20}", re.IGNORECASE)

def _sanitize(text: str, max_len: int = 500) -> str:
    if not text:
        return text
    # truncate first
    t = text[:max_len]
    # redact obvious secrets (best-effort)
    if _SENSITIVE_RE.search(t):
        return "[REDACTED: sensitive content stripped]"
    # also strip env-like lines
    for line in t.splitlines():
        if "TELEGRAM_BOT_TOKEN" in line or "DATABASE_URL" in line:
            return "[REDACTED]"
    return t


def _find_pid(pattern: str) -> int | None:
    try:
        import psutil  # type: ignore

        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmd = " ".join(p.info.get("cmdline") or [])
                if pattern in cmd or pattern in (p.info.get("name") or ""):
                    return int(p.info["pid"])
            except Exception:
                continue
    except Exception:
        pass
    return None


def _log_excerpt(path: Path, marker: str | None = None, max_chars: int = 400) -> str | None:
    try:
        if not path.exists():
            return None
        # read last 4KB safely
        data = path.read_bytes()[-4096:].decode(errors="replace")
        if marker and marker not in data:
            # return last lines even if marker missing
            pass
        # sanitize and bound
        excerpt = _sanitize(data[-max_chars:], max_len=max_chars)
        # keep last non-empty lines
        lines = [ln for ln in excerpt.splitlines() if ln.strip()]
        tail = "\n".join(lines[-3:])
        return _sanitize(tail, max_len=max_chars) if tail else None
    except Exception:
        return None


def _service_display(service: ServiceName) -> str:
    mapping = {
        ServiceName.GATEWAY: "IB Gateway",
        ServiceName.BACKEND: "Trading Backend",
        ServiceName.WEBHOOK: "Webhook Ingest",
        ServiceName.DEMO: "Demo Streaming",
        ServiceName.POSTGRES: "PostgreSQL",
        ServiceName.REDIS: "Redis",
    }
    return mapping.get(service, service.value)


# ---- checkers ----


class GatewayHealthChecker(ServiceHealthChecker):
    def __init__(self, settings: WatchdogSettings):
        self.settings = settings

    async def check(self) -> HealthResult:
        host = self.settings.gateway_host
        port = self.settings.gateway_port
        tcp_ok = await _tcp_open_async(host, port, timeout=1.0)
        if not tcp_ok:
            # distinguish Xvfb vs Gateway: check Xvfb display
            xvfb_pid = _find_pid("Xvfb :99")
            xvfb_detail = None
            if xvfb_pid is None:
                # check if Xvfb process missing at all
                try:
                    import psutil  # type: ignore

                    has_xvfb = any("Xvfb" in (p.info.get("name") or "") for p in psutil.process_iter(["name"]))
                    if not has_xvfb:
                        return HealthResult(
                            service=ServiceName.GATEWAY,
                            status=HealthStatus.FAILED,
                            liveness=HealthStatus.FAILED,
                            readiness=HealthStatus.FAILED,
                            detail=f"TCP {host}:{port} refused — Xvfb display :99 not running",
                            reason="xvfb_missing",
                            host=host,
                            port=port,
                            underlying_error="Xvfb process not found",
                            log_marker="Login has completed",
                            what_happened="IB Gateway API socket is unreachable and Xvfb display :99 is not running.",
                            impact="IBC cannot start Gateway GUI; Trading Backend cannot communicate with IBKR.",
                            trading_impact="Order execution is BLOCKED.",
                            operator_action="Inspect Xvfb + IBC + ib_gateway.log; process_manager will attempt restart.",
                        )
                except Exception:
                    pass
            return HealthResult(
                service=ServiceName.GATEWAY,
                status=HealthStatus.FAILED,
                liveness=HealthStatus.FAILED,
                readiness=HealthStatus.FAILED,
                detail=f"TCP {host}:{port} refused",
                reason="tcp_refused",
                host=host,
                port=port,
                underlying_error=f"ConnectionRefusedError: TCP {host}:{port} refused",
                what_happened="IB Gateway API socket is unreachable.",
                impact="Trading Backend cannot communicate with IBKR.",
                trading_impact="Order execution is BLOCKED.",
                operator_action="Check ib_gateway.log for login/auth errors; process_manager is restarting Xvfb+IBC+Gateway.",
            )
        # TCP open => liveness OK; readiness needs login marker
        readiness = HealthStatus.HEALTHY
        detail = f"TCP {host}:{port} open"
        log_excerpt = None
        missing_marker = False
        try:
            log_dir = Path("/home/tradingapp/storage/logs")
            today = time.strftime("%Y-%m-%d")
            candidates = [log_dir / today / "ib_gateway.log", Path("/home/tradingapp/storage/logs/ib_gateway.log")]
            for p in candidates:
                if p.exists():
                    data = p.read_bytes()[-8192:].decode(errors="replace")
                    if "Login has completed" not in data:
                        readiness = HealthStatus.DEGRADED
                        detail += " (login marker not seen)"
                        missing_marker = True
                        log_excerpt = _log_excerpt(p, max_chars=400)
                    else:
                        log_excerpt = None
                    break
        except Exception:
            pass

        if missing_marker:
            return HealthResult(
                service=ServiceName.GATEWAY,
                status=HealthStatus.DEGRADED,
                liveness=HealthStatus.HEALTHY,
                readiness=HealthStatus.DEGRADED,
                detail=detail,
                reason="login_marker_missing",
                host=host,
                port=port,
                log_marker="Login has completed",
                log_excerpt=log_excerpt,
                endpoint_url=f"tcp://{host}:{port}",
                what_happened="IB Gateway process is running but IBKR login completion was not detected.",
                impact="Gateway socket is open but session not authenticated; orders cannot be placed.",
                trading_impact="Trading is BLOCKED until login completes.",
                operator_action="Inspect ib_gateway.log + IBC auth/2FA; verify account/mode.",
            )

        gw_pid = _find_pid("ib_gateway") or _find_pid("IBGateway") or _find_pid("ibcstart")
        return HealthResult(
            service=ServiceName.GATEWAY,
            status=HealthStatus.HEALTHY if readiness == HealthStatus.HEALTHY else HealthStatus.DEGRADED,
            liveness=HealthStatus.HEALTHY,
            readiness=readiness,
            detail=detail,
            reason="healthy" if readiness == HealthStatus.HEALTHY else "login_marker_missing",
            host=host,
            port=port,
            pid=gw_pid,
            endpoint_url=f"tcp://{host}:{port}",
            log_marker="Login has completed",
            what_happened="IB Gateway is reachable and login completed." if readiness == HealthStatus.HEALTHY else "IB Gateway reachable but login marker missing.",
        )


class BackendHealthChecker(ServiceHealthChecker):
    def __init__(self, settings: WatchdogSettings):
        self.settings = settings

    async def check(self) -> HealthResult:
        host = self.settings.backend_host
        port = self.settings.backend_port
        url = f"http://{host}:{port}/health"
        ready_url = f"http://{host}:{port}/health/ready"
        pid = _find_pid("app.main") or _find_pid("uvicorn")  # best-effort
        ok, detail, latency = await _http_get(url, timeout=2.0)
        if ok:
            # readiness check via /health/ready (preferred) else system-monitor
            rok, rdetail, rlat = await _http_get(ready_url, timeout=2.0)
            if not rok:
                # fallback to system-monitor
                rok, rdetail, _ = await _http_get(f"http://{host}:{port}/api/v1/system-monitor", timeout=2.0)
                if not rok:
                    # alive but readiness unknown — treat as degraded with precise reason
                    return HealthResult(
                        service=ServiceName.BACKEND,
                        status=HealthStatus.DEGRADED,
                        liveness=HealthStatus.HEALTHY,
                        readiness=HealthStatus.DEGRADED,
                        latency_ms=latency,
                        detail=f"{detail} | readiness {rdetail}",
                        reason="readiness_failed",
                        host=host,
                        port=port,
                        pid=pid,
                        endpoint="/health/ready",
                        endpoint_url=ready_url,
                        underlying_error=_sanitize(rdetail),
                        what_happened="Trading Backend process is alive but is not ready for trading.",
                        impact="Execution API is up but readiness gate failed; trading must remain blocked.",
                        trading_impact="Trading execution is BLOCKED until readiness passes.",
                        operator_action="Check backend logs + Gateway/Postgres connectivity; waiting for Gateway recovery if applicable.",
                    )
                # system-monitor fallback detail
            # need to inspect readiness body for degraded reason
            # fetch ready body to extract reason
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    resp = await client.get(ready_url)
                    if resp.status_code == 200:
                        body = resp.json()
                        if body.get("status") == "degraded":
                            return HealthResult(
                                service=ServiceName.BACKEND,
                                status=HealthStatus.DEGRADED,
                                liveness=HealthStatus.HEALTHY,
                                readiness=HealthStatus.DEGRADED,
                                latency_ms=latency,
                                detail=_sanitize(str(body)),
                                reason="readiness_degraded",
                                host=host,
                                port=port,
                                pid=pid,
                                endpoint="/health/ready",
                                endpoint_url=ready_url,
                                underlying_error=_sanitize(str(body.get("reason", ""))),
                                what_happened="Trading Backend process is alive but is not ready for trading.",
                                impact="TWS/DB readiness check reported degraded.",
                                trading_impact="Trading is BLOCKED.",
                                operator_action="Inspect readiness reason; verify Gateway + PostgreSQL.",
                            )
            except Exception:
                pass
            return HealthResult(
                service=ServiceName.BACKEND,
                status=HealthStatus.HEALTHY,
                liveness=HealthStatus.HEALTHY,
                readiness=HealthStatus.HEALTHY,
                latency_ms=latency,
                detail=detail,
                reason="healthy",
                host=host,
                port=port,
                pid=pid,
                endpoint="/health",
                endpoint_url=url,
                what_happened="Trading Backend is healthy and ready.",
                impact="Execution workers can process signal_jobs.",
                trading_impact="Trading is READY (subject to safety gates).",
            )
        # HTTP failed — distinguish TCP vs HTTP
        if await _tcp_open_async(host, port):
            return HealthResult(
                service=ServiceName.BACKEND,
                status=HealthStatus.DEGRADED,
                liveness=HealthStatus.HEALTHY,
                readiness=HealthStatus.FAILED,
                detail=f"TCP open but {detail}",
                reason="http_failed_tcp_open",
                host=host,
                port=port,
                pid=pid,
                endpoint="/health",
                endpoint_url=url,
                underlying_error=_sanitize(detail),
                what_happened="Trading Backend port is open but HTTP health check failed.",
                impact="API is not responding on /health; execution may be degraded.",
                trading_impact="Trading may be BLOCKED.",
                operator_action="Check fastapi.log for crash/startup recovery errors.",
            )
        return HealthResult(
            service=ServiceName.BACKEND,
            status=HealthStatus.FAILED,
            liveness=HealthStatus.FAILED,
            readiness=HealthStatus.FAILED,
            detail=_sanitize(detail),
            reason="tcp_refused",
            host=host,
            port=port,
            pid=pid,
            endpoint="/health",
            endpoint_url=url,
            underlying_error=_sanitize(detail),
            what_happened="Trading Backend is no longer responding on port 8001.",
            impact="Execution API is unavailable and execution workers cannot process trading jobs.",
            trading_impact="Trading execution is BLOCKED.",
            operator_action="process_manager is responsible for restarting the Backend.",
        )


class WebhookHealthChecker(ServiceHealthChecker):
    def __init__(self, settings: WatchdogSettings):
        self.settings = settings

    async def check(self) -> HealthResult:
        host = self.settings.webhook_host
        port = self.settings.webhook_port
        url = f"http://{host}:{port}/health"
        ready_url = f"http://{host}:{port}/health/ready"
        pid = _find_pid("app.webhook_ingest") or _find_pid("webhook")
        ok, detail, latency = await _http_get(url, timeout=2.0)
        if ok:
            rok, rdetail, _ = await _http_get(ready_url, timeout=2.0)
            if not rok and "degraded" in rdetail.lower():
                return HealthResult(
                    service=ServiceName.WEBHOOK,
                    status=HealthStatus.DEGRADED,
                    liveness=HealthStatus.HEALTHY,
                    readiness=HealthStatus.DEGRADED,
                    latency_ms=latency,
                    detail=f"{detail} | ready {rdetail}",
                    reason="readiness_failed_postgres",
                    host=host,
                    port=port,
                    pid=pid,
                    dependency="postgres",
                    endpoint="/health/ready",
                    endpoint_url=ready_url,
                    underlying_error=_sanitize(rdetail),
                    what_happened="Webhook Ingest process is alive but PostgreSQL connectivity check failed.",
                    impact="TradingView webhooks cannot currently be accepted.",
                    trading_impact="Previously persisted signal_jobs remain in PostgreSQL; new webhooks may fail.",
                    operator_action="Check PostgreSQL + webhook.log.",
                )
            return HealthResult(
                service=ServiceName.WEBHOOK,
                status=HealthStatus.HEALTHY,
                liveness=HealthStatus.HEALTHY,
                readiness=HealthStatus.HEALTHY,
                latency_ms=latency,
                detail=detail,
                reason="healthy",
                host=host,
                port=port,
                pid=pid,
                endpoint="/health",
                endpoint_url=url,
                what_happened="Webhook Ingest is healthy.",
            )
        if await _tcp_open_async(host, port):
            return HealthResult(
                service=ServiceName.WEBHOOK,
                status=HealthStatus.DEGRADED,
                liveness=HealthStatus.HEALTHY,
                readiness=HealthStatus.FAILED,
                detail=f"TCP open but {detail}",
                reason="http_failed_tcp_open",
                host=host,
                port=port,
                pid=pid,
                endpoint="/health",
                endpoint_url=url,
                underlying_error=_sanitize(detail),
                what_happened="Webhook Ingest port is open but HTTP check failed.",
                impact="TradingView webhooks may be degraded.",
                operator_action="Check webhook.log.",
            )
        return HealthResult(
            service=ServiceName.WEBHOOK,
            status=HealthStatus.FAILED,
            liveness=HealthStatus.FAILED,
            readiness=HealthStatus.FAILED,
            detail=_sanitize(detail),
            reason="tcp_refused",
            host=host,
            port=port,
            pid=pid,
            endpoint="/health",
            endpoint_url=url,
            underlying_error=_sanitize(detail),
            what_happened="Webhook Ingest API is unavailable on port 8000.",
            impact="TradingView webhooks cannot currently be accepted; new webhook requests may fail while unavailable. Previously persisted signal_jobs remain in PostgreSQL.",
            trading_impact="No direct trading execution impact, but new signals will be missed.",
            operator_action="process_manager will restart Webhook Ingest.",
        )


class DemoHealthChecker(ServiceHealthChecker):
    def __init__(self, settings: WatchdogSettings):
        self.settings = settings

    async def check(self) -> HealthResult:
        host = self.settings.demo_host
        port = self.settings.demo_port
        url = f"http://{host}:{port}/health"
        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(url)
                elapsed = round((time.perf_counter() - start) * 1000, 1)
                if 200 <= resp.status_code < 300:
                    try:
                        data = resp.json()
                        if not data.get("redis", True):
                            return HealthResult(
                                service=ServiceName.DEMO,
                                status=HealthStatus.DEGRADED,
                                liveness=HealthStatus.HEALTHY,
                                readiness=HealthStatus.DEGRADED,
                                latency_ms=elapsed,
                                detail=f"HTTP {resp.status_code} redis degraded",
                                reason="redis_degraded",
                                host=host,
                                port=port,
                                dependency="redis",
                                underlying_error="Redis PING failed",
                                endpoint="/health",
                                endpoint_url=url,
                                what_happened="Demo Streaming health check failed — Redis PING failed.",
                                impact="Real-time dashboard streaming may be unavailable.",
                                trading_impact="None — execution pipeline does not depend on Demo Streaming.",
                                operator_action="systemd will restart demo-streaming.service; check Redis.",
                            )
                    except Exception:
                        pass
                    return HealthResult(
                        service=ServiceName.DEMO,
                        status=HealthStatus.HEALTHY,
                        liveness=HealthStatus.HEALTHY,
                        readiness=HealthStatus.HEALTHY,
                        latency_ms=elapsed,
                        detail=f"HTTP {resp.status_code}",
                        reason="healthy",
                        host=host,
                        port=port,
                        endpoint="/health",
                        endpoint_url=url,
                        what_happened="Demo Streaming is healthy.",
                    )
                return HealthResult(
                    service=ServiceName.DEMO,
                    status=HealthStatus.DEGRADED,
                    liveness=HealthStatus.HEALTHY,
                    readiness=HealthStatus.FAILED,
                    detail=f"HTTP {resp.status_code}",
                    reason="http_non_2xx",
                    host=host,
                    port=port,
                    endpoint="/health",
                    endpoint_url=url,
                    underlying_error=f"HTTP {resp.status_code}",
                    what_happened="Demo Streaming health check returned non-2xx.",
                    impact="Dashboard streaming may be unavailable.",
                    trading_impact="None — execution independent.",
                )
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
            if await _tcp_open_async(host, port):
                return HealthResult(
                    service=ServiceName.DEMO,
                    status=HealthStatus.DEGRADED,
                    liveness=HealthStatus.HEALTHY,
                    readiness=HealthStatus.FAILED,
                    detail=f"TCP open but {detail}",
                    reason="http_failed_tcp_open",
                    host=host,
                    port=port,
                    underlying_error=_sanitize(detail),
                    endpoint="/health",
                    endpoint_url=url,
                    what_happened="Demo Streaming port is open but HTTP check failed.",
                    impact="Dashboard may be degraded.",
                    trading_impact="None.",
                )
            return HealthResult(
                service=ServiceName.DEMO,
                status=HealthStatus.FAILED,
                liveness=HealthStatus.FAILED,
                readiness=HealthStatus.FAILED,
                detail=_sanitize(detail),
                reason="tcp_refused",
                host=host,
                port=port,
                underlying_error=_sanitize(detail),
                endpoint="/health",
                endpoint_url=url,
                what_happened="Demo Streaming is unavailable.",
                impact="Real-time dashboard streaming may be unavailable.",
                trading_impact="None — execution pipeline does not depend on Demo Streaming.",
                operator_action="systemd will restart demo-streaming.service.",
            )


class PostgresHealthChecker(ServiceHealthChecker):
    def __init__(self, settings: WatchdogSettings):
        self.settings = settings

    async def check(self) -> HealthResult:
        host = self.settings.postgres_host
        port = self.settings.postgres_port
        if not await _tcp_open_async(host, port, timeout=1.0):
            return HealthResult(
                service=ServiceName.POSTGRES,
                status=HealthStatus.FAILED,
                liveness=HealthStatus.FAILED,
                readiness=HealthStatus.FAILED,
                detail=f"TCP {host}:{port} refused",
                reason="tcp_refused",
                host=host,
                port=port,
                underlying_error=f"TCP connection to {host}:{port} failed: Connection refused",
                what_happened="PostgreSQL health check failed — TCP connection failed.",
                impact="Webhook persistence and trading execution database access are affected.",
                trading_impact="Trading execution is BLOCKED if DB unavailable.",
                operator_action="Manual PostgreSQL/container investigation required. No automatic recovery configured.",
            )
        url = self.settings.database_url
        engine: AsyncEngine | None = None
        try:
            engine = create_async_engine(url, pool_size=1, max_overflow=0, pool_pre_ping=False, pool_timeout=2.0)
            async with engine.connect() as conn:
                await asyncio.wait_for(conn.execute(text("SELECT 1")), timeout=2.0)
            return HealthResult(
                service=ServiceName.POSTGRES,
                status=HealthStatus.HEALTHY,
                liveness=HealthStatus.HEALTHY,
                readiness=HealthStatus.HEALTHY,
                detail="SELECT 1 ok",
                reason="healthy",
                host=host,
                port=port,
                endpoint="SELECT 1",
            )
        except TimeoutError:
            return HealthResult(
                service=ServiceName.POSTGRES,
                status=HealthStatus.DEGRADED,
                liveness=HealthStatus.HEALTHY,
                readiness=HealthStatus.FAILED,
                detail="SELECT 1 timeout",
                reason="sql_timeout",
                host=host,
                port=port,
                dependency="postgres",
                underlying_error="SELECT 1 timed out after 2s",
                what_happened="PostgreSQL TCP is open but SELECT 1 timed out.",
                impact="Database access is degraded.",
                operator_action="Check PostgreSQL load / connections.",
            )
        except Exception as exc:  # noqa: BLE001
            return HealthResult(
                service=ServiceName.POSTGRES,
                status=HealthStatus.DEGRADED,
                liveness=HealthStatus.HEALTHY,
                readiness=HealthStatus.FAILED,
                detail=_sanitize(f"TCP open but query failed: {exc}"),
                reason="sql_failed",
                host=host,
                port=port,
                dependency="postgres",
                underlying_error=_sanitize(str(exc)),
                what_happened="PostgreSQL TCP is open but SELECT 1 failed.",
                impact="Database access is degraded.",
                operator_action="Check PostgreSQL logs / credentials.",
            )
        finally:
            if engine is not None:
                try:
                    await engine.dispose()
                except Exception:
                    pass


class RedisHealthChecker(ServiceHealthChecker):
    def __init__(self, settings: WatchdogSettings):
        self.settings = settings

    async def check(self) -> HealthResult:
        host = self.settings.redis_host
        port = self.settings.redis_port
        tcp_ok = await _tcp_open_async(host, port, timeout=1.0)
        if not tcp_ok:
            return HealthResult(
                service=ServiceName.REDIS,
                status=HealthStatus.FAILED,
                liveness=HealthStatus.FAILED,
                readiness=HealthStatus.FAILED,
                detail=f"TCP {host}:{port} refused",
                reason="tcp_refused",
                host=host,
                port=port,
                underlying_error=f"TCP connection to {host}:{port} failed: Connection refused",
                what_happened="Redis is unavailable — TCP connection failed.",
                impact="Demo Streaming real-time updates are degraded.",
                trading_impact="None.",
                operator_action="Manual Redis investigation; no automatic recovery.",
            )
        try:
            from redis.asyncio import Redis as AsyncRedis  # type: ignore

            r = AsyncRedis.from_url(self.settings.redis_url)
            try:
                ok = await asyncio.wait_for(r.ping(), timeout=1.5)
                if ok:
                    return HealthResult(
                        service=ServiceName.REDIS,
                        status=HealthStatus.HEALTHY,
                        liveness=HealthStatus.HEALTHY,
                        readiness=HealthStatus.HEALTHY,
                        detail="PING ok",
                        reason="healthy",
                        host=host,
                        port=port,
                        endpoint="PING",
                    )
            finally:
                try:
                    await r.aclose()
                except Exception:
                    pass
        except TimeoutError:
            return HealthResult(
                service=ServiceName.REDIS,
                status=HealthStatus.DEGRADED,
                liveness=HealthStatus.HEALTHY,
                readiness=HealthStatus.FAILED,
                detail="PING timeout",
                reason="ping_timeout",
                host=host,
                port=port,
                underlying_error="PING timed out after 1.5s",
                what_happened="Redis TCP is open but PING timed out.",
                impact="Demo Streaming degraded.",
                trading_impact="None.",
            )
        except Exception as exc:  # noqa: BLE001
            return HealthResult(
                service=ServiceName.REDIS,
                status=HealthStatus.DEGRADED,
                liveness=HealthStatus.HEALTHY,
                readiness=HealthStatus.FAILED,
                detail=_sanitize(f"TCP open but ping failed: {exc}"),
                reason="ping_failed",
                host=host,
                port=port,
                underlying_error=_sanitize(str(exc)),
                what_happened="Redis is unavailable — PING failed.",
                impact="Demo Streaming real-time updates are degraded.",
                trading_impact="None.",
                operator_action="Check Redis logs.",
            )
        return HealthResult(
            service=ServiceName.REDIS,
            status=HealthStatus.DEGRADED,
            liveness=HealthStatus.HEALTHY,
            readiness=HealthStatus.FAILED,
            detail="TCP open but ping returned falsy",
            reason="ping_falsy",
            host=host,
            port=port,
        )
