#!/usr/bin/env python3
"""
process_manager.py

Supervises three processes for the One Alpha trading stack:
  1. Xvfb          - virtual framebuffer required by IB Gateway's GUI
  2. IB Gateway     - started via IBC (ibcstart.sh), depends on Xvfb/DISPLAY
  3. FastAPI server - uvicorn serving app.main:app

Design notes:
  - Xvfb and IB Gateway are treated as a dependent pair: if Xvfb dies,
    Gateway is restarted too (it needs a live DISPLAY).
  - FastAPI is independent and can be restarted on its own.
  - Each child is launched in its own process group (start_new_session=True)
    so we can cleanly signal the whole subtree (e.g. ibcstart.sh spawns
    Xvfb-using java processes underneath it).
  - Restarts are rate-limited (max N within a rolling window) to avoid
    crash-loops silently hammering IBKR's servers.
  - SIGTERM/SIGINT trigger an ordered graceful shutdown: FastAPI -> Gateway -> Xvfb.

Run this as the foreground process under systemd (Type=simple) or a
long-lived screen/tmux session -- it IS the supervisor, not a one-shot script.
"""

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

HOME = "/home/tradingapp"
LOG_DIR = Path(f"{HOME}/logs/process_manager")
LOG_DIR.mkdir(parents=True, exist_ok=True)

BACKEND_DIR = f"{HOME}/app/backend"
VENV_PYTHON = f"{BACKEND_DIR}/.venv/bin/python"
FASTAPI_HOST = "127.0.0.1"
FASTAPI_PORT = 8000
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

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "supervisor.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("process_manager")


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

        log_path = self.logfile or (LOG_DIR / f"{self.name}.log")
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


def fastapi_cmd():
    return [
        VENV_PYTHON, "-m", "uvicorn", "app.main:app",
        "--host", FASTAPI_HOST,
        "--port", str(FASTAPI_PORT),
    ]


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def fastapi_healthy() -> bool:
    """
    Prefer a real HTTP health check (should verify DB connectivity and IB
    session state inside the app) over a raw socket check. Falls back to
    the socket check if the health endpoint itself isn't reachable, so a
    missing/misconfigured endpoint doesn't make this always report unhealthy.
    """
    url = f"http://{FASTAPI_HOST}:{FASTAPI_PORT}{FASTAPI_HEALTH_PATH}"
    try:
        with urllib.request.urlopen(url, timeout=FASTAPI_HEALTH_TIMEOUT_SEC) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        # Endpoint exists but returned an error status -- treat as unhealthy.
        log.debug(f"Health endpoint returned HTTP {e.code}")
        return False
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        # Endpoint unreachable (not implemented, still starting, etc.) --
        # fall back to a basic port check so we don't false-negative on
        # deployments without a /health route.
        return port_open(FASTAPI_HOST, FASTAPI_PORT)


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
    def __init__(self):
        self.stopping = False

        self.xvfb = ManagedProcess(name="xvfb", build_cmd=xvfb_cmd)

        self.gateway = ManagedProcess(
            name="ib_gateway",
            build_cmd=ib_gateway_cmd,
            env_overrides={"DISPLAY": DISPLAY},
        )

        self.fastapi = ManagedProcess(
            name="fastapi",
            build_cmd=fastapi_cmd,
            cwd=BACKEND_DIR,
        )

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    # -- lifecycle ----------------------------------------------------------

    def start_all(self):
        log.info("=== Starting trading stack ===")

        clear_stale_xvfb_lock()
        self.xvfb.start()
        time.sleep(XVFB_SETTLE_SEC)
        if not self.xvfb.is_alive():
            log.error("Xvfb failed to start; aborting startup")
            sys.exit(1)

        self.gateway.start()

        if not Path(VENV_PYTHON).is_file():
            log.error(
                f"venv python not found at {VENV_PYTHON} -- check that the "
                f"virtualenv exists and the path is correct"
            )
            sys.exit(1)
        self.fastapi.start()

        log.info("=== Startup sequence complete, entering supervision loop ===")

    def _handle_signal(self, signum, _frame):
        log.info(f"Received signal {signum}, initiating graceful shutdown")
        self.stopping = True

    def shutdown_all(self):
        log.info("=== Shutting down trading stack ===")
        # Stop the order-facing service first, then Gateway, then Xvfb.
        self.fastapi.terminate()
        self.gateway.terminate()
        self.xvfb.terminate()
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
            clear_stale_xvfb_lock()
        proc.start()

        if also_restart:
            also_restart.record_restart()
            also_restart.terminate(grace_sec=5)
            time.sleep(XVFB_SETTLE_SEC if proc is self.xvfb else 0)
            also_restart.start()

        return True

    def run(self):
        self.start_all()

        try:
            while not self.stopping:
                time.sleep(POLL_INTERVAL_SEC)

                if self.stopping:
                    break

                # Xvfb + Gateway are a dependent pair.
                if not self.xvfb.is_alive():
                    log.warning("Xvfb is down -- restarting Xvfb and IB Gateway together")
                    ok = self._restart(self.xvfb, also_restart=self.gateway)
                    if not ok:
                        continue

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

                if not self.fastapi.is_alive():
                    log.warning("FastAPI process is down -- restarting FastAPI")
                    self._restart(self.fastapi)
                elif not fastapi_healthy():
                    # Process exists but isn't answering on its port yet/anymore.
                    # Give it a little time before treating as unhealthy (e.g.
                    # during its own startup) -- log only, don't restart every
                    # single poll interval.
                    log.debug("FastAPI process alive but port not yet accepting connections")

        finally:
            self.shutdown_all()


# ---------------------------------------------------------------------------

def main():
    log.info(f"process_manager starting, PID={os.getpid()}")
    Supervisor().run()
    log.info("process_manager exiting")


if __name__ == "__main__":
    main()