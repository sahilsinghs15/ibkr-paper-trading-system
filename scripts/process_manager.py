#!/usr/bin/env python3
"""
DEPRECATED — do not run this as the production supervisor.

Systemd units own restarts: trading-backend.service, webhook-ingest.service,
ibgateway.service. `process-manager.service` must stay disabled. Watchdog
observes/notifies only and does not call systemctl.

process_manager.py

Supervises the One Alpha trading stack as three selectable CLI groups:

  webhook  - uvicorn ``app.webhook_ingest:app`` on :8000 (Postgres only)
  gateway  - Xvfb + IB Gateway (IBC); Gateway needs a live DISPLAY
  fastapi  - uvicorn ``app.main:app`` on :8001 (implies gateway)

Examples::

    python process_manager.py                    # all three groups
    python process_manager.py webhook            # ingest only
    python process_manager.py fastapi            # gateway pair + trading
    python process_manager.py webhook fastapi    # ingest + trading

Selected children run only on **weekdays 09:30–16:00 America/New_York**.
The supervisor process itself stays up 24/7 so the next session starts
automatically. Outside the window, enabled children are stopped and not
restarted until the next open.

Design notes:
  - Xvfb and IB Gateway are treated as a dependent pair: if Xvfb dies,
    Gateway is restarted too (it needs a live DISPLAY).
  - Trading FastAPI starts only after IBC logs "Login has completed" *and* the
    Gateway API port accepts TCP. Port-up during authentication is not
    enough (TWS 502 / handshake timeout). The adapter does not reconnect,
    so a Gateway bounce also restarts the trading app after the new login.
  - Each child is launched in its own process group (start_new_session=True)
    so we can cleanly signal the whole subtree (e.g. ibcstart.sh spawns
    Xvfb-using java processes underneath it).
  - Restarts are rate-limited (max N within a rolling window) to avoid
    crash-loops silently hammering IBKR's servers.
  - SIGTERM/SIGINT trigger an ordered graceful shutdown of enabled groups:
    Trading FastAPI -> Gateway -> Xvfb -> Webhook ingest.

Run this as the foreground process under systemd (Type=simple) or a
long-lived screen/tmux session -- it IS the supervisor, not a one-shot script.

Logs: ``/home/tradingapp/storage/logs/{YYYY-MM-DD}/`` only
(supervisor.log, webhook.log, xvfb.log, ib_gateway.log, fastapi.log).
Not ``/home/tradingapp/logs``.
"""

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
import logging
import urllib.request
import urllib.error
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional, Callable
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Configuration -- adjust paths/ports here
# ---------------------------------------------------------------------------

HOME = os.environ.get("HOME_DIR", "/home/tradingapp")
# Same tree as app.core.logger: storage/logs/{YYYY-MM-DD}/{name}.log
STORAGE_LOG_ROOT = Path(os.environ.get("STORAGE_LOG_ROOT", f"{HOME}/storage/logs"))
DEMO_RESTART_TRIGGER_FILE = Path(os.environ.get("DEMO_RESTART_TRIGGER_FILE", f"{HOME}/storage/state/restart_demo.trigger"))
BACKEND_RESTART_TRIGGER_FILE = Path(os.environ.get("BACKEND_RESTART_TRIGGER_FILE", f"{HOME}/storage/state/restart_backend.trigger"))

BACKEND_DIR = f"{HOME}/app/backend"
VENV_PYTHON = f"{BACKEND_DIR}/.venv/bin/python"
FASTAPI_HOST = "127.0.0.1"
INGEST_PORT = 8000
TRADING_PORT = 8001
# If the app exposes a real health endpoint (checks DB + IB session, not just
# "process is up"), use it. Falls back to a raw port check if unreachable.
FASTAPI_HEALTH_PATH = "/health"
FASTAPI_HEALTH_TIMEOUT_SEC = 2.0

# --- Expected daily IB Gateway restart window --------------------------------
# IBC's AutoRestart should be configured to fire around 3:52 AM ET (the
# 3:50-4:00 AM ET gap between the overnight and regular US CFD sessions).
# The supervisor uses this window to distinguish an *expected* daily restart
# from a genuine crash, so expected restarts don't eat into the crash-loop
# budget or fire noisy warnings.
GATEWAY_EXPECTED_RESTART_TZ = ZoneInfo("America/New_York")
GATEWAY_EXPECTED_RESTART_TIME = dtime(3, 52)   # must match IBC's AutoRestart time
GATEWAY_EXPECTED_RESTART_WINDOW_MIN = 12       # tolerance either side, in minutes

# --- Trading session window (weekdays only) ----------------------------------
SESSION_TZ = ZoneInfo("America/New_York")
SESSION_START = dtime(9, 30)
SESSION_END = dtime(16, 0)

VALID_GROUPS = frozenset({"webhook", "gateway", "fastapi"})
ALL_GROUPS = VALID_GROUPS

DISPLAY_NUM = "99"
DISPLAY = f":{DISPLAY_NUM}"
XVFB_SCREEN = "1024x768x24"

IBC_SCRIPT = f"{HOME}/ibc/scripts/ibcstart.sh"
IBC_WAIT_ARG = "1045"          # first positional arg to ibcstart.sh, as given
TWS_PATH = f"{HOME}/Jts"
TWS_SETTINGS_PATH = f"{HOME}/Jts"
IBC_PATH = f"{HOME}/ibc"
IBC_INI = f"{HOME}/ibc/config.ini"

# How often the supervisor loop checks process health
POLL_INTERVAL_SEC = 5

# Restart rate limiting: at most MAX_RESTARTS within RESTART_WINDOW_SEC
MAX_RESTARTS = 5
RESTART_WINDOW_SEC = 600  # 10 minutes

# Grace period given to a process on SIGTERM before SIGKILL
SHUTDOWN_GRACE_SEC = 15

# Seconds to wait after starting Xvfb before starting Gateway
XVFB_SETTLE_SEC = 2

# Paper Gateway API socket (must match IBC / backend IBKR_PORT on this host)
GATEWAY_API_HOST = "127.0.0.1"
GATEWAY_API_PORT = 4001
# IBC writes this after a successful paper/live logon (see ib_gateway.log)
GATEWAY_LOGIN_MARKER = "Login has completed"
GATEWAY_READY_TIMEOUT_SEC = 180
GATEWAY_READY_POLL_SEC = 1.0
# API bind can lag IBC's login line by a few seconds
GATEWAY_API_SETTLE_SEC = 5.0

# ---------------------------------------------------------------------------
# Logging — storage/logs/{YYYY-MM-DD}/ only (do not write /home/tradingapp/logs)
# ---------------------------------------------------------------------------

def dated_log_dir() -> Path:
    """Return ``storage/logs/{YYYY-MM-DD}``, creating it if needed."""
    date_str = datetime.now().astimezone().strftime("%Y-%m-%d")
    path = STORAGE_LOG_ROOT / date_str
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


class DatedFileHandler(logging.FileHandler):
    """Append to ``storage/logs/{YYYY-MM-DD}/{basename}``, reopening at midnight."""

    def __init__(self, basename: str) -> None:
        self._basename = basename
        self._date = datetime.now().astimezone().strftime("%Y-%m-%d")
        try:
            target_path = dated_log_dir() / basename
            super().__init__(target_path, mode="a", encoding="utf-8")
        except OSError:
            # Fallback to devnull if log dir is unwritable
            super().__init__(os.devnull, mode="a", encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        if today != self._date:
            self.close()
            try:
                self.baseFilename = str(dated_log_dir() / self._basename)
            except OSError:
                self.baseFilename = os.devnull
            self._date = today
            try:
                self.stream = self._open()
            except OSError:
                pass
        super().emit(record)


log = logging.getLogger("process_manager")


def setup_logging() -> None:
    """Configure supervisor logging handlers if not already configured."""
    if not log.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            handlers=[
                DatedFileHandler("supervisor.log"),
                logging.StreamHandler(sys.stdout),
            ],
        )


# ---------------------------------------------------------------------------
# Managed process wrapper
# ---------------------------------------------------------------------------

@dataclass
class ManagedProcess:
    name: str
    build_cmd: Callable[[], list]
    cwd: Optional[str] = None
    env_overrides: dict = field(default_factory=dict)
    logfile: Optional[Path] = None
    proc: Optional[subprocess.Popen] = None
    restart_times: deque = field(default_factory=lambda: deque(maxlen=MAX_RESTARTS))

    def start(self):
        cmd = self.build_cmd()
        env = os.environ.copy()
        env.update(self.env_overrides)

        log_path = self.logfile or (dated_log_dir() / f"{self.name}.log")
        log_fh = open(log_path, "a", buffering=1)

        log.info(f"Starting {self.name}: {' '.join(cmd)}")
        self.proc = subprocess.Popen(
            cmd,
            cwd=self.cwd,
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # own process group -> clean signaling
        )
        log.info(f"{self.name} started with PID {self.proc.pid}")

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def record_restart(self):
        self.restart_times.append(time.time())

    def restart_budget_exceeded(self) -> bool:
        now = time.time()
        recent = [t for t in self.restart_times if now - t < RESTART_WINDOW_SEC]
        return len(recent) >= MAX_RESTARTS

    def terminate(self, grace_sec: int = SHUTDOWN_GRACE_SEC):
        if not self.proc:
            return
        pgid = None
        try:
            pgid = os.getpgid(self.proc.pid)
        except ProcessLookupError:
            return

        log.info(f"Sending SIGTERM to {self.name} (pgid={pgid})")
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return

        deadline = time.time() + grace_sec
        while time.time() < deadline:
            if self.proc.poll() is not None:
                log.info(f"{self.name} exited cleanly")
                return
            time.sleep(0.5)

        log.warning(f"{self.name} did not exit within {grace_sec}s, sending SIGKILL")
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------

def xvfb_cmd():
    return ["Xvfb", DISPLAY, "-screen", "0", XVFB_SCREEN]


def clear_stale_xvfb_lock():
    """
    Xvfb refuses to bind a display if a lock file from a previous
    (possibly crashed/unclean) run is still present, even when nothing
    is actually holding the display. Clear it before every Xvfb start
    so a stale lock doesn't silently kill startup.
    """
    lock_path = Path(f"/tmp/.X{DISPLAY_NUM}-lock")
    socket_path = Path(f"/tmp/.X11-unix/X{DISPLAY_NUM}")

    for p in (lock_path, socket_path):
        if p.exists():
            log.warning(f"Removing stale Xvfb lock/socket: {p}")
            try:
                p.unlink()
            except OSError as e:
                log.error(f"Could not remove {p}: {e}")


def kill_orphaned_xvfb():
    """
    If a previous process_manager run was force-killed (kill -9, double
    Ctrl+C) rather than stopped gracefully, its Xvfb child can be left
    running and genuinely holding the display -- lock-file cleanup alone
    won't fix that, since the conflict is real, not stale. Find and
    terminate any Xvfb process already bound to our target display
    before attempting to start a new one.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"Xvfb {DISPLAY} "],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        log.debug("pgrep not available -- skipping orphaned Xvfb check")
        return

    pids = [int(p) for p in result.stdout.split() if p.strip()]
    if not pids:
        return

    for pid in pids:
        log.warning(
            f"Found orphaned Xvfb process PID {pid} already bound to "
            f"{DISPLAY} (likely left over from an unclean shutdown) -- terminating it"
        )
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    time.sleep(1)

    # Escalate if still alive after the grace period
    for pid in pids:
        try:
            os.kill(pid, 0)  # existence check
            log.warning(f"PID {pid} still alive after SIGTERM, sending SIGKILL")
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def ib_gateway_cmd():
    return [
        IBC_SCRIPT,
        IBC_WAIT_ARG,
        "--gateway",
        f"--tws-path={TWS_PATH}",
        f"--tws-settings-path={TWS_SETTINGS_PATH}",
        f"--ibc-path={IBC_PATH}",
        f"--ibc-ini={IBC_INI}",
    ]


def webhook_ingest_cmd():
    return [
        VENV_PYTHON, "-m", "uvicorn", "app.webhook_ingest:app",
        "--host", FASTAPI_HOST,
        "--port", str(INGEST_PORT),
    ]


def fastapi_cmd():
    return [
        VENV_PYTHON, "-m", "uvicorn", "app.main:app",
        "--host", FASTAPI_HOST,
        "--port", str(TRADING_PORT),
    ]


def gateway_log_path() -> Path:
    return dated_log_dir() / "ib_gateway.log"


def log_file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def log_contains_since(path: Path, marker: str, offset: int) -> bool:
    """True if *marker* appears in *path* at or after byte *offset*."""
    try:
        with open(path, "r", errors="replace") as fh:
            fh.seek(offset)
            return marker in fh.read()
    except FileNotFoundError:
        return False


def wait_for_gateway_ready(
    *,
    log_path: Path,
    log_offset: int,
    is_alive: Callable[[], bool],
    port_check: Callable[[], bool],
    should_abort: Callable[[], bool] | None = None,
    timeout_sec: float = GATEWAY_READY_TIMEOUT_SEC,
    poll_sec: float = GATEWAY_READY_POLL_SEC,
    settle_sec: float = GATEWAY_API_SETTLE_SEC,
    sleeper: Callable[[float], None] = time.sleep,
) -> bool:
    """Block until IBC login completed (this session) and the API port is up.

    A previous run's "Login has completed" line is ignored via *log_offset*.
    """
    deadline = time.time() + timeout_sec
    logged_in = False
    port_up = False

    while time.time() < deadline:
        if should_abort is not None and should_abort():
            log.info("Aborting Gateway ready wait (shutdown requested)")
            return False
        if not is_alive():
            log.error("IB Gateway process exited before login completed")
            return False

        if not logged_in:
            logged_in = log_contains_since(
                log_path, GATEWAY_LOGIN_MARKER, log_offset
            )
            if logged_in:
                log.info("IBC reported Login has completed")

        if not port_up:
            port_up = port_check()
            if port_up:
                log.info(
                    f"Gateway API listening on {GATEWAY_API_HOST}:{GATEWAY_API_PORT}"
                )

        if logged_in and port_up:
            if settle_sec > 0:
                sleeper(settle_sec)
            if not is_alive():
                log.error("IB Gateway process died during API settle")
                return False
            if not port_check():
                log.warning("Gateway API port dropped during settle; continuing to wait")
                port_up = False
                continue
            log.info("Gateway is logged in and API port is ready")
            return True

        sleeper(poll_sec)

    log.error(
        f"Gateway not ready after {timeout_sec:.0f}s "
        f"(login={logged_in} port={port_up})"
    )
    return False


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_healthy(host: str, port: int) -> bool:
    """HTTP GET /health with port fallback."""
    url = f"http://{host}:{port}{FASTAPI_HEALTH_PATH}"
    try:
        with urllib.request.urlopen(url, timeout=FASTAPI_HEALTH_TIMEOUT_SEC) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        log.debug(f"Health endpoint returned HTTP {e.code}")
        return False
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return port_open(host, port)


def ingest_healthy() -> bool:
    """Health check for webhook ingest on :8000."""
    return _http_healthy(FASTAPI_HOST, INGEST_PORT)


def fastapi_healthy() -> bool:
    """Health check for trading FastAPI on :8001."""
    return _http_healthy(FASTAPI_HOST, TRADING_PORT)


def is_trading_session(now: Optional[datetime] = None) -> bool:
    """True on weekdays between 09:30 and 16:00 US Eastern (inclusive start)."""
    now_et = (now or datetime.now(SESSION_TZ)).astimezone(SESSION_TZ)
    if now_et.weekday() >= 5:
        return False
    return SESSION_START <= now_et.time() < SESSION_END


def resolve_enabled_groups(names: list[str]) -> frozenset[str]:
    """Normalize CLI group names; default all; fastapi implies gateway."""
    if not names:
        return ALL_GROUPS
    unknown = set(names) - VALID_GROUPS
    if unknown:
        raise ValueError(
            f"unknown group(s): {', '.join(sorted(unknown))}; "
            f"valid: {', '.join(sorted(VALID_GROUPS))}"
        )
    enabled = set(names)
    if "fastapi" in enabled and "gateway" not in names:
        log.info("fastapi requires gateway -- enabling gateway group")
    if "fastapi" in enabled:
        enabled.add("gateway")
    return frozenset(enabled)


def parse_cli_groups(argv: Optional[list[str]] = None) -> frozenset[str]:
    """Parse positional CLI args into the enabled group set."""
    parser = argparse.ArgumentParser(
        description=(
            "Supervise webhook ingest, IB Gateway, and/or trading FastAPI. "
            "Selected groups run weekdays 09:30-16:00 ET only; omit args for all."
        ),
    )
    parser.add_argument(
        "groups",
        nargs="*",
        choices=sorted(VALID_GROUPS),
        metavar="GROUP",
        help="webhook, gateway, and/or fastapi (default: all)",
    )
    args = parser.parse_args(argv)
    return resolve_enabled_groups(args.groups)


def is_within_expected_gateway_restart_window(now: Optional[datetime] = None) -> bool:
    """
    True if the current time falls within the tolerance window around
    IBC's configured daily AutoRestart time (in ET). Used to distinguish
    an expected daily Gateway bounce from a genuine crash.
    """
    now_et = (now or datetime.now(GATEWAY_EXPECTED_RESTART_TZ)).astimezone(
        GATEWAY_EXPECTED_RESTART_TZ
    )
    target_today = now_et.replace(
        hour=GATEWAY_EXPECTED_RESTART_TIME.hour,
        minute=GATEWAY_EXPECTED_RESTART_TIME.minute,
        second=0,
        microsecond=0,
    )
    delta_min = abs((now_et - target_today).total_seconds()) / 60.0
    return delta_min <= GATEWAY_EXPECTED_RESTART_WINDOW_MIN


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------

class Supervisor:
    def __init__(self, enabled: frozenset[str] = ALL_GROUPS):
        self.enabled = enabled
        self.stopping = False
        self._session_active = False

        self.xvfb = ManagedProcess(name="xvfb", build_cmd=xvfb_cmd)

        self.gateway = ManagedProcess(
            name="ib_gateway",
            build_cmd=ib_gateway_cmd,
            env_overrides={"DISPLAY": DISPLAY},
        )

        self.webhook = ManagedProcess(
            name="webhook",
            build_cmd=webhook_ingest_cmd,
            cwd=BACKEND_DIR,
        )

        self.fastapi = ManagedProcess(
            name="fastapi",
            build_cmd=fastapi_cmd,
            cwd=BACKEND_DIR,
        )

        # Byte offset / path of ib_gateway.log for the current Gateway session.
        self._gateway_log_offset = 0
        self._gateway_log_path = dated_log_dir() / "ib_gateway.log"
        self._gateway_epoch = 0
        self._fastapi_epoch = 0
        self._demo_restarted_epoch = 0
        self._pending_backend_restart = False

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        # SIGUSR1 → safe manual backend-only restart (does not affect Gateway/Webhook)
        try:
            signal.signal(signal.SIGUSR1, self._handle_backend_restart_signal)
        except (AttributeError, ValueError, OSError):
            pass

    @property
    def _webhook_enabled(self) -> bool:
        return "webhook" in self.enabled

    @property
    def _gateway_enabled(self) -> bool:
        return "gateway" in self.enabled

    @property
    def _fastapi_enabled(self) -> bool:
        return "fastapi" in self.enabled

    def _should_abort_gateway_wait(self) -> bool:
        return self.stopping or not is_trading_session()

    def _any_enabled_child_alive(self) -> bool:
        if self._webhook_enabled and self.webhook.is_alive():
            return True
        if self._gateway_enabled and (
            self.xvfb.is_alive() or self.gateway.is_alive()
        ):
            return True
        if self._fastapi_enabled and self.fastapi.is_alive():
            return True
        return False

    def _start_gateway(self):
        """Start Gateway and remember the log offset for this session."""
        log_path = dated_log_dir() / "ib_gateway.log"
        self.gateway.logfile = log_path
        self._gateway_log_path = log_path
        self._gateway_log_offset = log_file_size(log_path)
        self.gateway.start()
        self._gateway_epoch += 1

    def _gateway_is_ready(self) -> bool:
        return (
            self.gateway.is_alive()
            and log_contains_since(
                self._gateway_log_path,
                GATEWAY_LOGIN_MARKER,
                self._gateway_log_offset,
            )
            and port_open(GATEWAY_API_HOST, GATEWAY_API_PORT)
        )

    def _wait_for_gateway_ready(self) -> bool:
        log.info(
            f"Waiting for IB Gateway login and API {GATEWAY_API_HOST}:{GATEWAY_API_PORT}"
        )
        return wait_for_gateway_ready(
            log_path=self._gateway_log_path,
            log_offset=self._gateway_log_offset,
            is_alive=self.gateway.is_alive,
            port_check=lambda: port_open(GATEWAY_API_HOST, GATEWAY_API_PORT),
            should_abort=self._should_abort_gateway_wait,
        )

    def _ensure_fastapi(self):
        """Start or reconnect FastAPI only after this Gateway session is ready."""
        if not self._fastapi_enabled:
            return
        if not self._gateway_is_ready():
            return
        if self.fastapi.is_alive() and self._fastapi_epoch == self._gateway_epoch:
            return
        if not Path(VENV_PYTHON).is_file():
            log.error(
                f"venv python not found at {VENV_PYTHON} -- check that the "
                f"virtualenv exists and the path is correct"
            )
            return
        if self.fastapi.is_alive():
            log.info(
                "Restarting FastAPI so it can handshake with the new Gateway session"
            )
            self.fastapi.terminate(grace_sec=5)
        self.fastapi.start()
        self._fastapi_epoch = self._gateway_epoch

    def _ensure_demo_streaming_reconnected(self):
        """Trigger demo-streaming.service restart once when FastAPI is healthy for current epoch."""
        if not self._fastapi_enabled or not self.fastapi.is_alive():
            return
        if self._demo_restarted_epoch == self._fastapi_epoch:
            return
        if fastapi_healthy():
            log.info(
                "FastAPI is healthy — triggering demo-streaming.service restart via trigger file for epoch %d",
                self._fastapi_epoch,
            )
            try:
                DEMO_RESTART_TRIGGER_FILE.parent.mkdir(parents=True, exist_ok=True)
                DEMO_RESTART_TRIGGER_FILE.touch()
                log.info(
                    "Successfully touched demo streaming restart trigger file %s for FastAPI epoch %d",
                    DEMO_RESTART_TRIGGER_FILE,
                    self._fastapi_epoch,
                )
                self._demo_restarted_epoch = self._fastapi_epoch
            except Exception as exc:
                log.error("Exception touching demo streaming restart trigger file: %s", exc)

    # -- lifecycle ----------------------------------------------------------

    def _ensure_webhook(self):
        """Start webhook ingest if not running (independent of Gateway)."""
        if not self._webhook_enabled:
            return
        if self.webhook.is_alive():
            return
        if not Path(VENV_PYTHON).is_file():
            log.error(
                f"venv python not found at {VENV_PYTHON} -- cannot start webhook ingest"
            )
            return
        self.webhook.start()

    def _start_gateway_stack(self) -> bool:
        """Start Xvfb + IB Gateway and wait for login. Returns False on failure."""
        kill_orphaned_xvfb()
        clear_stale_xvfb_lock()
        if not self.xvfb.is_alive():
            self.xvfb.start()
            time.sleep(XVFB_SETTLE_SEC)
        if not self.xvfb.is_alive():
            log.error("Xvfb failed to start; aborting gateway startup")
            return False

        if not self.gateway.is_alive():
            self._start_gateway()
        if not self._wait_for_gateway_ready():
            log.error(
                "Gateway did not become ready in time; FastAPI will start "
                "once IBC login completes and the API port is up"
            )
            return False
        return True

    def start_all(self):
        groups = ", ".join(sorted(self.enabled))
        log.info(f"=== Starting selected groups: {groups} ===")

        self._ensure_webhook()

        if self._gateway_enabled:
            self._start_gateway_stack()
            if self._fastapi_enabled:
                self._ensure_fastapi()

    def _handle_signal(self, signum, _frame):
        log.info(f"Received signal {signum}, initiating graceful shutdown")
        self.stopping = True

    def _handle_backend_restart_signal(self, signum, _frame):
        log.info(f"Received signal {signum}, scheduling backend-only restart")
        self._pending_backend_restart = True

    def _handle_backend_restart(self):
        """Restart only Trading Backend (fastapi) without affecting Gateway/Webhook — safe manual operation."""
        if not self._fastapi_enabled:
            log.warning("Backend restart requested but fastapi group not enabled")
            return False
        log.info("=== Manual backend-only restart requested ===")
        # Only restart fastapi; gateway/webhook remain untouched (independent PGIDs)
        ok = self._restart(self.fastapi)
        if ok:
            log.info(f"Backend restart completed (PID {self.fastapi.proc.pid if self.fastapi.proc else '?'}) — gateway/webhook unaffected")
            # Trigger demo streaming refresh exactly once for this manual epoch
            # Use same trigger file mechanism as automatic _ensure_demo_streaming_reconnected (epoch-gated)
            try:
                # Force demo to restart on next healthy check by resetting its epoch marker
                # Then _ensure_demo_streaming_reconnected will touch trigger file once
                # Alternatively, touch directly now for immediacy
                DEMO_RESTART_TRIGGER_FILE.parent.mkdir(parents=True, exist_ok=True)
                DEMO_RESTART_TRIGGER_FILE.touch()
                log.info(f"Triggered demo streaming restart for manual backend epoch")
                # Mark demo as needing refresh — set to previous epoch so next check will also handle
                # but avoid double-touch: we already touched, so set demo_restarted to current-1
                self._demo_restarted_epoch = self._fastapi_epoch - 1 if self._fastapi_epoch > 0 else -1
            except Exception as exc:
                log.error(f"Failed to trigger demo restart after manual backend restart: {exc}")
        return ok

    def _check_backend_restart_trigger(self):
        """Poll for operator trigger file: storage/state/restart_backend.trigger"""
        try:
            if BACKEND_RESTART_TRIGGER_FILE.exists():
                log.info(f"Detected backend restart trigger file {BACKEND_RESTART_TRIGGER_FILE}")
                try:
                    BACKEND_RESTART_TRIGGER_FILE.unlink()
                except OSError:
                    pass
                self._pending_backend_restart = True
        except Exception:
            pass
        if self._pending_backend_restart:
            self._pending_backend_restart = False
            self._handle_backend_restart()

    def stop_children(self):
        """Stop enabled children without exiting the supervisor."""
        if not self._any_enabled_child_alive():
            return
        log.info("=== Stopping selected groups ===")
        if self._fastapi_enabled:
            self.fastapi.terminate()
        if self._gateway_enabled:
            self.gateway.terminate()
            self.xvfb.terminate()
        if self._webhook_enabled:
            self.webhook.terminate()

    def shutdown_all(self):
        log.info("=== Shutting down trading stack ===")
        self.stop_children()
        log.info("=== Shutdown complete ===")

    # -- supervision loop -----------------------------------------------------

    def _restart(
        self,
        proc: ManagedProcess,
        also_restart: Optional[ManagedProcess] = None,
        count_against_budget: bool = True,
    ):
        if count_against_budget and proc.restart_budget_exceeded():
            log.critical(
                f"{proc.name} exceeded restart budget "
                f"({MAX_RESTARTS} restarts within {RESTART_WINDOW_SEC}s). "
                f"Not restarting automatically -- manual intervention required."
            )
            return False

        if count_against_budget:
            proc.record_restart()
        proc.terminate(grace_sec=5)  # in case it's half-alive
        if proc is self.xvfb:
            kill_orphaned_xvfb()
            clear_stale_xvfb_lock()
        if proc is self.gateway:
            self._start_gateway()
        else:
            proc.start()

        if also_restart:
            also_restart.record_restart()
            also_restart.terminate(grace_sec=5)
            time.sleep(XVFB_SETTLE_SEC if proc is self.xvfb else 0)
            if also_restart is self.gateway:
                self._start_gateway()
            else:
                also_restart.start()

        return True

    def _supervise_session(self):
        """Restart enabled children that die during an open trading session."""
        if self._gateway_enabled:
            # Xvfb + Gateway are a dependent pair.
            if not self.xvfb.is_alive():
                log.warning("Xvfb is down -- restarting Xvfb and IB Gateway together")
                ok = self._restart(self.xvfb, also_restart=self.gateway)
                if not ok:
                    return
                self._wait_for_gateway_ready()

            elif not self.gateway.is_alive():
                # Gateway can exit on its own (e.g. IBC-driven daily
                # auto-restart, forced logoff, auth failure). Xvfb is
                # fine, so just bring Gateway back up.
                if is_within_expected_gateway_restart_window():
                    log.info(
                        "IB Gateway is down within the expected daily "
                        "AutoRestart window -- treating as scheduled, "
                        "restarting without counting against crash budget"
                    )
                    self._restart(self.gateway, count_against_budget=False)
                else:
                    log.warning(
                        "IB Gateway is down outside the expected restart "
                        "window -- treating as unexpected, restarting"
                    )
                    self._restart(self.gateway)
                self._wait_for_gateway_ready()

        if self._webhook_enabled:
            if not self.webhook.is_alive():
                log.warning("Webhook ingest is down -- restarting")
                self._restart(self.webhook)
            elif not ingest_healthy():
                log.debug(
                    "Webhook ingest process alive but port not yet accepting connections"
                )

        if self._fastapi_enabled:
            if self._gateway_is_ready():
                self._ensure_fastapi()
            elif not self.fastapi.is_alive():
                log.debug(
                    "Trading FastAPI is down; waiting for Gateway login before starting it"
                )
            elif not fastapi_healthy():
                log.debug(
                    "Trading FastAPI process alive but port not yet accepting connections"
                )

            if self.fastapi.is_alive() and fastapi_healthy():
                self._ensure_demo_streaming_reconnected()

    def run(self):
        log.info(
            f"Supervisor enabled groups: {sorted(self.enabled)}; "
            f"session window weekdays {SESSION_START.strftime('%H:%M')}-"
            f"{SESSION_END.strftime('%H:%M')} {SESSION_TZ.key}"
        )

        try:
            while not self.stopping:
                # Check for operator-requested backend-only restart (file or SIGUSR1) — isolated, no Gateway/Webhook impact
                self._check_backend_restart_trigger()

                in_session = is_trading_session()

                if in_session:
                    if not self._session_active:
                        self._session_active = True
                        log.info(
                            f"=== Trading session open === groups="
                            f"{sorted(self.enabled)}"
                        )
                        self.start_all()
                    else:
                        self._supervise_session()
                else:
                    if self._session_active:
                        self._session_active = False
                        log.info("=== Trading session closed ===")
                    if self._any_enabled_child_alive():
                        self.stop_children()

                time.sleep(POLL_INTERVAL_SEC)

        finally:
            self.shutdown_all()


# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None):
    setup_logging()
    enabled = parse_cli_groups(argv)
    log.info(
        f"process_manager starting, PID={os.getpid()}, "
        f"groups={sorted(enabled)}"
    )
    Supervisor(enabled).run()
    log.info("process_manager exiting")


if __name__ == "__main__":
    main(sys.argv[1:])