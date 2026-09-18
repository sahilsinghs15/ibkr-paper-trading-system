"""Authentication / authorization security events for the audit trail.

These establish the security context around consequential actions (login ->
settings change -> order -> kill switch). Noise control: repeated denials and
ended-session token reuse are throttled per key so a misbehaving client cannot
flood the trail; the number of suppressed repeats is carried on the next record.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from starlette.requests import Request

from app.audit.context import AuditActor, Principal, principal_from_request
from app.audit.recorder import audit_entry, get_audit_recorder
from app.audit.sanitize import optional_text
from app.audit.taxonomy import ActorType, AuditAction, AuditResult

logger = logging.getLogger(__name__)

_THROTTLE_SECONDS = 60.0
_THROTTLE_MAX_KEYS = 5000
_throttle: dict[tuple[Any, ...], tuple[float, int]] = {}


def _admit(key: tuple[Any, ...]) -> int | None:
    """Return suppressed-count if this key may be recorded now, else None."""
    now = time.monotonic()
    last = _throttle.get(key)
    if last is not None and now - last[0] < _THROTTLE_SECONDS:
        _throttle[key] = (last[0], last[1] + 1)
        return None
    if len(_throttle) >= _THROTTLE_MAX_KEYS:
        _throttle.clear()
    _throttle[key] = (now, 0)
    return last[1] if last is not None else 0


def reset_throttle() -> None:
    _throttle.clear()


def _actor_from_principal(principal: Principal | None) -> AuditActor:
    if principal is None:
        return AuditActor.anonymous()
    return AuditActor(
        actor_type=ActorType.USER,
        user_id=principal.user_id,
        email=principal.email,
        role=principal.role,
        auth_method=principal.auth_method,
        session=principal.session,
        token_issued_at=principal.token_issued_at,
    )


async def record_login_failed(request: Request, *, attempted_email: str, reason: str) -> None:
    email = optional_text(attempted_email.strip().lower(), 255) or "(empty)"
    await get_audit_recorder(request).record_committed(
        audit_entry(
            request,
            action=AuditAction.LOGIN_FAILED,
            actor=AuditActor.anonymous(),
            summary=f"Failed login for {email} ({reason})",
            result=AuditResult.DENIED,
            result_reason=reason,
            target_type="USER_ACCOUNT",
            target_id=email,
            parameters={"attempted_email": email},
        )
    )


async def record_session_token_rejected(
    request: Request, *, user_id: int, email: str | None, session_id: str, reason: str
) -> None:
    suppressed = _admit(("session_rejected", session_id))
    if suppressed is None:
        return
    await get_audit_recorder(request).record_committed(
        audit_entry(
            request,
            action=AuditAction.SESSION_TOKEN_REJECTED,
            actor=AuditActor.anonymous(),
            summary=f"Token for ended session presented (user {email or user_id})",
            result=AuditResult.DENIED,
            result_reason=reason,
            target_type="AUTH_SESSION",
            target_id=session_id,
            parameters={"claimed_user_id": user_id, "claimed_email": email},
            related={"session_id": session_id},
            extra_context={"suppressed_repeats_since_last_record": suppressed},
        )
    )


async def record_access_denied(request: Request, *, status_code: int, detail: str) -> None:
    principal = principal_from_request(request)
    user_key = principal.user_id if principal else None
    path = request.url.path
    suppressed = _admit(("denied", user_key, request.method, path))
    if suppressed is None:
        return
    actor = _actor_from_principal(principal)
    who = principal.email if principal else "unauthenticated caller"
    await get_audit_recorder(request).record_committed(
        audit_entry(
            request,
            action=AuditAction.ACCESS_DENIED,
            actor=actor,
            summary=f"Access denied for {who}: {request.method} {path}",
            result=AuditResult.DENIED,
            result_reason=detail,
            target_type="API_ENDPOINT",
            target_id=f"{request.method} {path}"[:128],
            parameters={"status_code": status_code},
            extra_context={"suppressed_repeats_since_last_record": suppressed},
        )
    )
