"""Service control API — safe, allowlisted systemd operations for Admin UI.

Only fixed services and actions are allowed. No arbitrary systemctl command is ever executed.
"""

import asyncio
import logging
import subprocess
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_admin
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

    try:
        # Use systemctl directly, no shell, fixed unit/action — safe
        cmd = ["systemctl", action, unit]
        result = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            logger.error("Service control failed %s %s: %s", action, unit, result.stderr.strip()[:500])
            raise HTTPException(status_code=500, detail=f"Failed to {action} {unit}: {result.stderr.strip()[:200]}")
        return {"service": service, "unit": unit, "action": action, "result": "ok", "output": result.stdout.strip()[:500]}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Service control exception for %s %s", action, unit)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get(
    "/allowed",
    summary="List allowed services and actions (admin only)",
)
async def list_allowed(
    _admin: Annotated[UserModel, Depends(require_admin)],
):
    return {"services": list(ALLOWED_SERVICES.keys()), "actions": sorted(ALLOWED_ACTIONS), "mapping": ALLOWED_SERVICES}
