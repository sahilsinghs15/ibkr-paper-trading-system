"""Emergency Kill Switch Webhook router definition.

Provides an external pre-flight webhook to arm the authoritative account Kill Switch
state on EC2 without executing broker flatten orders.
"""

import hmac
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models.account import AccountModel
from app.db.session import get_db_session
from app.schemas.config_schemas import (
    EmergencyKillSwitchRequest,
    EmergencyKillSwitchResponse,
)
from app.services.kill_switch import KillSwitchService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["emergency"])


def _verify_emergency_killswitch_auth(request: Request) -> None:
    """Validate Bearer authentication secret for emergency kill switch webhook.

    Uses constant-time comparison. Fails closed (401) if secret is unconfigured when enabled.
    Never logs the secret or Authorization header.
    """
    settings = get_settings()
    if not settings.emergency_killswitch_auth_enabled:
        logger.info("Emergency kill switch authentication is disabled (EMERGENCY_KILLSWITCH_AUTH_ENABLED=false)")
        return

    expected_secret = settings.emergency_killswitch_auth_secret
    if not expected_secret:
        logger.warning(
            "Emergency kill switch request rejected: EMERGENCY_KILLSWITCH_AUTH_SECRET is not configured."
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Emergency kill switch authentication not configured.",
        )

    auth_header = request.headers.get("Authorization")
    if not auth_header:
        logger.warning("Unauthorized emergency kill switch request: missing Authorization header")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Missing Authorization header.",
        )

    parts = auth_header.strip().split(" ", 1)
    if len(parts) != 2 or parts[0] != "Bearer":
        logger.warning("Unauthorized emergency kill switch request: malformed Authorization header")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Malformed Authorization header. Expected Bearer token.",
        )

    incoming_token = parts[1]
    if not hmac.compare_digest(
        expected_secret.encode("utf-8"), incoming_token.encode("utf-8")
    ):
        logger.warning("Unauthorized emergency kill switch request: invalid token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Invalid emergency authentication secret.",
        )


@router.post(
    "/emergency-kill-switch",
    response_model=EmergencyKillSwitchResponse,
    status_code=status.HTTP_200_OK,
    summary="Arm existing EC2 account Kill Switch from external emergency trigger",
    description=(
        "Authenticates external emergency kill switch request and arms the existing EC2 "
        "account Kill Switch state. Does NOT submit broker orders or perform position flattening on EC2."
    ),
)
async def emergency_kill_switch_endpoint(
    body: EmergencyKillSwitchRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> EmergencyKillSwitchResponse:
    """Enforce auth -> resolve IBKR account -> arm existing Kill Switch state -> return HTTP 200."""
    # 1. Enforce authentication BEFORE any database mutation or state change
    _verify_emergency_killswitch_auth(request)

    clean_ibkr_id = body.ibkr_account_id.strip()
    if not clean_ibkr_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ibkr_account_id must be non-empty.",
        )

    # 2. Resolve IBKR account ID string against existing account configuration
    stmt = select(AccountModel).where(func.upper(AccountModel.ibkr_account) == clean_ibkr_id.upper())
    account = (await session.execute(stmt)).scalars().first()

    if account is None:
        logger.warning("Emergency kill switch failed: IBKR account '%s' not found.", clean_ibkr_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Account with IBKR identifier '{clean_ibkr_id}' not found.",
        )

    # 3. Arm EXISTING account Kill Switch state (no IBKR order execution)
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise HTTPException(
            status_code=503,
            detail="Session factory is unavailable.",
        )

    order_manager = getattr(request.app.state, "order_manager", None)
    kill_switch_svc = KillSwitchService(
        session_factory=session_factory,
        order_manager=order_manager,
    )

    try:
        _op, created_new = await kill_switch_svc.arm_account_kill_switch_only(
            account_id=account.id,
            requested_by="emergency_webhook",
        )
    except Exception as exc:
        logger.exception("Failed to arm emergency kill switch for account_id=%s", account.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to persist emergency kill switch state.",
        ) from exc

    if created_new:
        message = "Emergency kill switch activated for account"
    else:
        message = "Kill switch was already active for account"

    return EmergencyKillSwitchResponse(
        success=True,
        ibkr_account_id=account.ibkr_account,
        kill_switch_active=True,
        message=message,
    )
