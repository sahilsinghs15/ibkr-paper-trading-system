"""Service control API — safe, allowlisted systemd operations for Admin UI.

Only fixed services and actions are allowed. No arbitrary systemctl command is ever executed.
"""

import asyncio
import logging
import subprocess
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import require_admin
from app.audit.context import actor_for_user
from app.audit.recorder import audit_entry, get_audit_recorder
from app.audit.taxonomy import AuditAction, AuditResult
from app.db.models.user import UserModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/service-control", tags=["service-control"])

# Fixed allowlists — no user-supplied string is ever passed to shell
ALLOWED_SERVICES: dict[str, str] = {
    "ibgateway": "ibgateway.service",
    "backend": "trading-backend.service",
    "webhook": "webhook-ingest.service",
    "watchdog": "watchdog.service",
    # demo is intentionally not controlled via this API to avoid demo→backend loop
}

ALLOWED_ACTIONS: set[str] = {"start", "stop", "restart", "status"}

# The unit hosting this API process.
SELF_UNIT = "trading-backend.service"

LIFECYCLE_AUDIT_ACTIONS: dict[str, AuditAction] = {
    "start": AuditAction.SERVICE_START,
    "stop": AuditAction.SERVICE_STOP,
    "restart": AuditAction.SERVICE_RESTART,
}

ServiceKey = Literal["ibgateway", "backend", "webhook", "watchdog"]
ActionKey = Literal["start", "stop", "restart", "status"]


@router.post(
    "/{service}/{action}",
    summary="Control a trading service (admin only, allowlisted)",
    description="Safe service control: only ibgateway, backend, webhook, watchdog with start/stop/restart/status. No arbitrary command.",
)
async def control_service(
    service: ServiceKey,
    action: ActionKey,
    request: Request,
    _admin: Annotated[UserModel, Depends(require_admin)],
):
    # Validate against allowlists (FastAPI Literal already does, but double-check)
    if service not in ALLOWED_SERVICES:
        raise HTTPException(status_code=400, detail=f"Service '{service}' not allowed")
    if action not in ALLOWED_ACTIONS:
        raise HTTPException(status_code=400, detail=f"Action '{action}' not allowed")

    unit = ALLOWED_SERVICES[service]

    # For status, we just query, not control
    if action == "status":
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["systemctl", "is-active", unit],
                capture_output=True,
                text=True,
                timeout=5,
            )
            is_active = result.stdout.strip()
            # Also get show for more detail
            show = await asyncio.to_thread(
                subprocess.run,
                ["systemctl", "show", unit, "--property=ActiveState", "--property=SubState", "--property=MainPID"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return {"service": service, "unit": unit, "action": action, "active": is_active, "details": show.stdout.strip()}
        except Exception as exc:
            logger.exception("Service status check failed for %s", unit)
            raise HTTPException(status_code=500, detail=str(exc))

    # For start/stop/restart, execute systemctl with fixed args, no shell
    # Watchdog stop is allowed but UI must warn that monitoring will disappear
    if service == "watchdog" and action == "stop":
        logger.warning("Admin requested watchdog stop — monitoring will be unavailable until restarted")

    # Stopping/restarting the unit that hosts this API terminates this process.
    # --no-block makes systemctl return once the job is queued instead of
    # waiting on our own shutdown (which would otherwise hang this request).
    self_terminating = unit == SELF_UNIT and action in ("stop", "restart")
    before_state = await _unit_state(unit)
    entry = audit_entry(
        request,
        action=LIFECYCLE_AUDIT_ACTIONS[action],
        actor=actor_for_user(request, _admin),
        summary=f"Service {action}: {unit}",
        target_type="SERVICE",
        target_id=unit,
        parameters={"service": service, "unit": unit, "action": action},
        before_state=before_state,
        related={"service": service, "unit": unit},
    )
    entry.extra_context = {"self_terminating": self_terminating}

    # ORDERING GUARANTEE: operation() commits the PENDING audit row in its own
    # transaction and only then yields. systemctl is never invoked unless that
    # commit succeeded (required=True fails closed with HTTP 503).
    async with get_audit_recorder(request).operation(entry, required=True) as audit_op:
        cmd = ["systemctl", action, unit]
        if self_terminating:
            cmd = ["systemctl", "--no-block", action, unit]
        try:
            # Use systemctl directly, no shell, fixed unit/action — safe
            result = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception as exc:
            logger.exception("Service control exception for %s %s", action, unit)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if result.returncode != 0:
            logger.error("Service control failed %s %s: %s", action, unit, result.stderr.strip()[:500])
            raise HTTPException(status_code=500, detail=f"Failed to {action} {unit}: {result.stderr.strip()[:200]}")
        if self_terminating:
            audit_op.set_outcome(
                AuditResult.ACCEPTED,
                reason="systemd job queued (--no-block); this process will now shut down.",
                after={"systemd_job": "queued", "returncode": result.returncode},
            )
        else:
            audit_op.set_outcome(
                AuditResult.SUCCEEDED,
                after={**(await _unit_state(unit) or {}), "returncode": result.returncode},
            )
        return {"service": service, "unit": unit, "action": action, "result": "ok", "output": result.stdout.strip()[:500]}


async def _unit_state(unit: str) -> dict[str, str] | None:
    """Best-effort ActiveState/SubState/MainPID of a unit (fixed args, no shell)."""
    try:
        show = await asyncio.to_thread(
            subprocess.run,
            ["systemctl", "show", unit, "--property=ActiveState", "--property=SubState", "--property=MainPID"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("Could not read unit state for %s", unit)
        return None
    state: dict[str, str] = {}
    for line in (show.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if sep and key in ("ActiveState", "SubState", "MainPID"):
            state[key] = value.strip()
    return state or None


@router.get(
    "/allowed",
    summary="List allowed services and actions (admin only)",
)
async def list_allowed(
    _admin: Annotated[UserModel, Depends(require_admin)],
):
    return {"services": list(ALLOWED_SERVICES.keys()), "actions": sorted(ALLOWED_ACTIONS), "mapping": ALLOWED_SERVICES}
