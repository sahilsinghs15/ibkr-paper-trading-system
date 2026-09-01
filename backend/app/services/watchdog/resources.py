"""Host resource monitoring — CPU, RAM, storage, inodes with hysteresis and dedup.

Reuses psutil / statvfs, thresholds are configuration-driven, state is NORMAL→WARNING→CRITICAL→RECOVERED
with hysteresis to avoid spam. Only notifies on meaningful transitions.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import psutil

from app.services.watchdog.config import WatchdogSettings

logger = logging.getLogger(__name__)


class ResourceType(str, Enum):
    CPU = "cpu"
    MEMORY = "memory"
    STORAGE = "storage"
    INODES = "inodes"


class ResourceState(str, Enum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass
class ResourceThresholds:
    warning: float
    critical: float
    recovery: float  # must be < warning to provide hysteresis


@dataclass
class ResourceMetrics:
    type: ResourceType
    usage_percent: float
    total_bytes: int | None = None
    used_bytes: int | None = None
    available_bytes: int | None = None
    extra: dict | None = None  # e.g., load_avg, cpu_count


@dataclass
class ResourceResult:
    type: ResourceType
    state: ResourceState
    metrics: ResourceMetrics
    is_transition: bool = False
    previous_state: ResourceState | None = None


def _get_thresholds(settings: WatchdogSettings, rtype: ResourceType) -> ResourceThresholds:
    # Config-driven thresholds with sensible defaults (hysteresis: recovery < warning)
    if rtype == ResourceType.CPU:
        return ResourceThresholds(
            warning=getattr(settings, "cpu_warning_threshold", 80.0),
            critical=getattr(settings, "cpu_critical_threshold", 90.0),
            recovery=getattr(settings, "cpu_recovery_threshold", 75.0),
        )
    if rtype == ResourceType.MEMORY:
        return ResourceThresholds(
            warning=getattr(settings, "memory_warning_threshold", 80.0),
            critical=getattr(settings, "memory_critical_threshold", 90.0),
            recovery=getattr(settings, "memory_recovery_threshold", 75.0),
        )
    if rtype == ResourceType.STORAGE:
        return ResourceThresholds(
            warning=getattr(settings, "storage_warning_threshold", 80.0),
            critical=getattr(settings, "storage_critical_threshold", 90.0),
            recovery=getattr(settings, "storage_recovery_threshold", 75.0),
        )
    if rtype == ResourceType.INODES:
        return ResourceThresholds(
            warning=getattr(settings, "inodes_warning_threshold", 80.0),
            critical=getattr(settings, "inodes_critical_threshold", 90.0),
            recovery=getattr(settings, "inodes_recovery_threshold", 75.0),
        )
    return ResourceThresholds(warning=80.0, critical=90.0, recovery=75.0)


def _resolve_storage_mount() -> Path:
    """Dynamically resolve the filesystem containing /home/tradingapp (fallback to /)."""
    candidates = [Path("/home/tradingapp"), Path("/home"), Path("/")]
    for p in candidates:
        try:
            if p.exists():
                return p
        except Exception:
            pass
    return Path("/")


class ResourceMonitor:
    """Tracks resource states with hysteresis and deduplication.

    Only emits notifications on transitions: NORMAL→WARNING→CRITICAL→RECOVERED.
    Remains in same state while high → no spam. Recovery only when below recovery threshold.
    """

    def __init__(self, settings: WatchdogSettings):
        self.settings = settings
        self._states: dict[ResourceType, ResourceState] = {
            ResourceType.CPU: ResourceState.NORMAL,
            ResourceType.MEMORY: ResourceState.NORMAL,
            ResourceType.STORAGE: ResourceState.NORMAL,
            ResourceType.INODES: ResourceState.NORMAL,
        }
        self._last_metrics: dict[ResourceType, ResourceMetrics] = {}
        # For testing injection
        self._cpu_percent_fn: Callable[[], float] | None = None
        self._check_interval = getattr(settings, "resource_check_interval_seconds", 30.0)
        self._last_check_ts: float = 0.0

    def _collect_cpu(self) -> ResourceMetrics:
        if self._cpu_percent_fn:
            usage = self._cpu_percent_fn()
        else:
            usage = psutil.cpu_percent(interval=None)
        # Normalize None
        usage = float(usage) if usage is not None else 0.0
        try:
            load1, load5, load15 = os.getloadavg()
        except Exception:
            load1, load5, load15 = 0.0, 0.0, 0.0
        cpu_count = psutil.cpu_count() or 1
        # Top CPU processes (high-signal, not noisy — keep lightweight, top 3)
        top_procs = []
        try:
            # Prime cpu_percent for all processes (first call returns 0.0, so we do two-phase)
            procs = list(psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]))
            # Brief sleep to let cpu_percent calculate if needed (non-blocking, 0.1s)
            # Instead, use already cached values; for accuracy, we sort by current cpu_percent
            for p in procs:
                try:
                    info = p.info
                    # Sanitize name (don't leak cmdline secrets)
                    name = (info.get("name") or "unknown")[:30]
                    # Redact if name contains sensitive
                    low = name.lower()
                    if any(k in low for k in ["bot_token", "password", "secret", "api_key"]):
                        name = "[REDACTED]"
                    top_procs.append({
                        "pid": info.get("pid"),
                        "name": name,
                        "cpu_percent": round(info.get("cpu_percent") or 0.0, 1),
                        "memory_percent": round(info.get("memory_percent") or 0.0, 1),
                    })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            top_procs = sorted(top_procs, key=lambda x: x["cpu_percent"], reverse=True)[:3]
        except Exception:
            top_procs = []
        return ResourceMetrics(
            type=ResourceType.CPU,
            usage_percent=round(usage, 1),
            extra={"load_avg": [round(load1,2), round(load5,2), round(load15,2)], "cpu_count": cpu_count, "top_processes": top_procs},
        )

    def _collect_memory(self) -> ResourceMetrics:
        vmem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        return ResourceMetrics(
            type=ResourceType.MEMORY,
            usage_percent=round(vmem.percent, 1),
            total_bytes=vmem.total,
            used_bytes=vmem.used,
            available_bytes=vmem.available,
            extra={"swap_percent": round(swap.percent,1), "swap_total": swap.total},
        )

    def _collect_storage(self) -> ResourceMetrics:
        mount = _resolve_storage_mount()
        usage = psutil.disk_usage(str(mount))
        return ResourceMetrics(
            type=ResourceType.STORAGE,
            usage_percent=round(usage.percent, 1),
            total_bytes=usage.total,
            used_bytes=usage.used,
            available_bytes=usage.free,
            extra={"mount": str(mount), "filesystem": mount.as_posix()},
        )

    def _collect_inodes(self) -> ResourceMetrics | None:
        mount = _resolve_storage_mount()
        try:
            stat = os.statvfs(str(mount))
            # f_files total inodes, f_favail available
            total = stat.f_files
            avail = stat.f_favail
            if total == 0:
                return None
            used = total - stat.f_bfree  # f_bfree is free blocks, but for inodes use f_ffree? Use f_favail
            # For inodes: used = total - f_ffree
            try:
                free_inodes = stat.f_ffree
                used_inodes = total - free_inodes
            except AttributeError:
                used_inodes = total - avail
            percent = round((used_inodes / total * 100) if total else 0.0, 1)
            return ResourceMetrics(
                type=ResourceType.INODES,
                usage_percent=percent,
                total_bytes=total,  # misuse for inode count, but for reporting
                used_bytes=used_inodes,
                available_bytes=free_inodes if 'free_inodes' in locals() else avail,
                extra={"mount": str(mount)},
            )
        except Exception as exc:
            logger.debug("Inode check failed for %s: %s", mount, exc)
            return None

    def _evaluate_state(self, rtype: ResourceType, usage: float, current: ResourceState) -> ResourceState:
        thr = _get_thresholds(self.settings, rtype)
        # Hysteresis: only go to WARNING/CRITICAL when crossing up, only recover when below recovery
        if current == ResourceState.NORMAL:
            if usage >= thr.critical:
                return ResourceState.CRITICAL
            if usage >= thr.warning:
                return ResourceState.WARNING
            return ResourceState.NORMAL
        if current == ResourceState.WARNING:
            if usage >= thr.critical:
                return ResourceState.CRITICAL
            if usage < thr.recovery:
                return ResourceState.NORMAL
            return ResourceState.WARNING
        if current == ResourceState.CRITICAL:
            if usage < thr.recovery:
                return ResourceState.NORMAL
            if usage < thr.warning:
                # Critical -> Warning when below warning but above recovery? Use recovery as single threshold for now
                # To avoid flapping, stay critical until recovery
                return ResourceState.CRITICAL
            if usage < thr.critical and usage >= thr.warning:
                # Stay critical until recovery, not warning
                return ResourceState.CRITICAL
            return ResourceState.CRITICAL
        return current

    def check_all(self) -> list[ResourceResult]:
        """Collect and evaluate all resources, returning results with transition flag."""
        results: list[ResourceResult] = []
        for rtype in [ResourceType.CPU, ResourceType.MEMORY, ResourceType.STORAGE, ResourceType.INODES]:
            try:
                if rtype == ResourceType.CPU:
                    metrics = self._collect_cpu()
                elif rtype == ResourceType.MEMORY:
                    metrics = self._collect_memory()
                elif rtype == ResourceType.STORAGE:
                    metrics = self._collect_storage()
                elif rtype == ResourceType.INODES:
                    metrics = self._collect_inodes()
                    if metrics is None:
                        continue
                else:
                    continue
                self._last_metrics[rtype] = metrics
                prev = self._states[rtype]
                new_state = self._evaluate_state(rtype, metrics.usage_percent, prev)
                is_trans = new_state != prev
                if is_trans:
                    self._states[rtype] = new_state
                results.append(ResourceResult(type=rtype, state=new_state, metrics=metrics, is_transition=is_trans, previous_state=prev if is_trans else None))
            except Exception as exc:
                logger.exception("Resource check failed for %s: %s", rtype.value, exc)
        return results

    def check_if_due(self) -> bool:
        now = time.monotonic()
        if now - self._last_check_ts >= self._check_interval:
            self._last_check_ts = now
            return True
        return False

    def get_state(self, rtype: ResourceType) -> ResourceState:
        return self._states.get(rtype, ResourceState.NORMAL)

    def get_metrics(self, rtype: ResourceType) -> ResourceMetrics | None:
        return self._last_metrics.get(rtype)

    # For testing: allow injecting cpu percent
    def set_cpu_percent_fn(self, fn: Callable[[], float] | None):
        self._cpu_percent_fn = fn
