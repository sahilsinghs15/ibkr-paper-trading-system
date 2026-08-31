"""Trading safety gates — fail-closed, never assume safe."""

from __future__ import annotations

import logging

import httpx

from app.services.watchdog.config import WatchdogSettings
from app.services.watchdog.models import SafetyGateResult

logger = logging.getLogger(__name__)


class SafetyGateChecker:
    """Verifies complete trading safety before TRADING_READY.

    Gates (all must be SAFE, UNKNOWN treated as UNSAFE):
      - system_monitor (overall + gateway/postgres alerts)
      - kill_switch (any account armed)
      - baskets (any BASKET_CRITICAL)
      - trading_mode (paper vs live — reported, not blocking unless unknown)
      - recovery (implicit via baskets)
    """

    def __init__(self, settings: WatchdogSettings):
        self.settings = settings
        self._base = f"http://{self.settings.backend_host}:{self.settings.backend_port}"

    async def check(self) -> SafetyGateResult:
        gates: dict[str, str] = {}  # gate -> SAFE/UNSAFE/UNKNOWN
        failures: list[str] = []

        # Gate 1: system-monitor
        gates["system_monitor"] = "UNKNOWN"
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{self._base}/api/v1/system-monitor")
                if resp.status_code != 200:
                    failures.append(f"system-monitor HTTP {resp.status_code}")
                    gates["system_monitor"] = "UNSAFE"
                else:
                    data = resp.json()
                    overall = data.get("overall_status", "")
                    if overall == "CRITICAL":
                        alerts = data.get("alerts", [])
                        for a in alerts:
                            comp = a.get("component", "")
                            if comp in ("IB Gateway", "PostgreSQL"):
                                failures.append(f"system-monitor CRITICAL: {a.get('message','')}")
                        gates["system_monitor"] = "UNSAFE" if failures else "UNKNOWN"
                    else:
                        gates["system_monitor"] = "SAFE"
        except Exception as exc:  # noqa: BLE001
            failures.append(f"system-monitor unreachable: {exc}")
            gates["system_monitor"] = "UNKNOWN"

        # Gate 2: kill switch — query accounts
        gates["kill_switch"] = "UNKNOWN"
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{self._base}/api/v1/config/accounts")
                if resp.status_code != 200:
                    failures.append(f"kill-switch check HTTP {resp.status_code}")
                    gates["kill_switch"] = "UNKNOWN"
                else:
                    data = resp.json()
                    accounts = data.get("accounts", data if isinstance(data, list) else [])
                    armed = [a for a in accounts if a.get("kill_switch_active")]
                    if armed:
                        ids = ", ".join(str(a.get("ibkr_account") or a.get("id")) for a in armed)
                        failures.append(f"kill switch ACTIVE for: {ids}")
                        gates["kill_switch"] = "UNSAFE"
                    else:
                        gates["kill_switch"] = "SAFE"
        except Exception as exc:  # noqa: BLE001
            failures.append(f"kill-switch check failed: {exc}")
            gates["kill_switch"] = "UNKNOWN"

        # Gate 3: baskets — any BASKET_CRITICAL
        gates["baskets"] = "UNKNOWN"
        try:
            # need account list — reuse same accounts fetch if available, else fetch again
            ibkr_accounts: list[str] = []
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    resp = await client.get(f"{self._base}/api/v1/config/accounts")
                    if resp.status_code == 200:
                        data = resp.json()
                        accts = data.get("accounts", data if isinstance(data, list) else [])
                        ibkr_accounts = [a.get("ibkr_account") for a in accts if a.get("ibkr_account")]
            except Exception:
                pass

            if not ibkr_accounts:
                # cannot determine accounts → UNKNOWN → fail closed
                failures.append("baskets check: cannot determine ibkr_accounts")
                gates["baskets"] = "UNKNOWN"
            else:
                found_critical = False
                for acct in ibkr_accounts:
                    try:
                        async with httpx.AsyncClient(timeout=3.0) as client:
                            resp = await client.get(f"{self._base}/api/v1/baskets/critical", params={"ibkr_account": acct})
                            if resp.status_code == 200:
                                data = resp.json()
                                incidents = data.get("incidents", [])
                                if incidents:
                                    found_critical = True
                                    failures.append(f"BASKET_CRITICAL for {acct}: {len(incidents)} incident(s)")
                            elif resp.status_code != 404:
                                failures.append(f"baskets/critical HTTP {resp.status_code} for {acct}")
                    except Exception as exc:  # noqa: BLE001
                        failures.append(f"baskets check for {acct} failed: {exc}")
                if found_critical:
                    gates["baskets"] = "UNSAFE"
                elif any("baskets" in f for f in failures):
                    gates["baskets"] = "UNKNOWN"
                else:
                    gates["baskets"] = "SAFE"
        except Exception as exc:  # noqa: BLE001
            failures.append(f"baskets gate error: {exc}")
            gates["baskets"] = "UNKNOWN"

        # Gate 4: trading mode — derived from ibkr_port (paper 7497/4002 vs live)
        gates["trading_mode"] = "SAFE"
        try:
            gw_port = self.settings.gateway_port
            if gw_port not in (4002, 7497, 7496, 4001):
                failures.append(f"trading mode UNKNOWN: gateway port {gw_port} not recognized")
                gates["trading_mode"] = "UNKNOWN"
            # else treat port 4002/7497 as paper (safe to report), 4001/7496 as live (still safe if intended)
            # No blocking on mode alone unless unknown
        except Exception as exc:  # noqa: BLE001
            failures.append(f"trading-mode check error: {exc}")
            gates["trading_mode"] = "UNKNOWN"

        # Determine overall: any UNSAFE or UNKNOWN → not safe (fail closed)
        unsafe_or_unknown = [k for k, v in gates.items() if v != "SAFE"]
        passed = len(unsafe_or_unknown) == 0
        if not passed and not failures:
            failures.append(f"safety gates not all SAFE: {gates}")

        return SafetyGateResult(
            passed=passed,
            failures=failures,
            details="; ".join(failures) if failures else "all gates SAFE",
            gates=gates,
        )
