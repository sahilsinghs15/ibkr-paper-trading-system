"""Authentication API routes."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_authenticated_user
from app.audit.context import (
    AuditActor,
    SessionInfo,
    actor_for_user,
    build_request_context,
    principal_from_request,
)
from app.audit.recorder import audit_entry, get_audit_recorder
from app.audit.security_events import record_login_failed
from app.audit.taxonomy import ActorType, AuditAction, AuditResult
from app.core.config import get_settings
from app.core.security import create_access_token, create_sse_token, verify_password
from app.db.models.audit import AuthSessionModel
from app.db.models.user import UserModel
from app.db.session import get_db_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    id: int
    email: str
    role: str
    is_active: bool
    ibkr_account_id: int | None = None
    ibkr_account: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class LogoutResponse(BaseModel):
    session_ended: bool


AUTH_METHOD_PASSWORD = "password"


class SseTokenResponse(BaseModel):
    sse_token: str
    token_type: str = "bearer"
    expires_in_seconds: int = 300


@router.post("/login", response_model=TokenResponse)
async def login(
    req: LoginRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> TokenResponse:
    """Authenticate user with email and password and issue JWT token.

    A server-side auth session is created and its id embedded as the signed
    ``sid`` claim; the session row and the LOGIN_SUCCEEDED audit record commit
    atomically. Failed attempts are audited (the password is never recorded).
    """
    email_clean = req.email.strip().lower()
    result = await session.execute(
        select(UserModel).where(UserModel.email == email_clean)
    )
    user = result.scalar_one_or_none()

    if user is None or not verify_password(req.password, user.password_hash):
        await record_login_failed(
            request,
            attempted_email=email_clean,
            reason="UNKNOWN_ACCOUNT" if user is None else "BAD_PASSWORD",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        await record_login_failed(
            request, attempted_email=email_clean, reason="INACTIVE_ACCOUNT"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )

    settings = get_settings()
    session_id = uuid.uuid4()
    issued_at = datetime.now(UTC)
    expires_at = issued_at + timedelta(minutes=settings.jwt_access_token_expire_minutes)
    request_ctx = build_request_context(request)

    token = create_access_token(
        data={
            "sub": str(user.id),
            "role": user.role,
            "email": user.email,
            "sid": str(session_id),
            "amr": AUTH_METHOD_PASSWORD,
        },
        expires_delta=expires_at - issued_at,
    )

    auth_session = AuthSessionModel(
        id=session_id,
        user_id=user.id,
        user_email=user.email,
        user_role=user.role,
        auth_method=AUTH_METHOD_PASSWORD,
        expires_at=expires_at,
        login_ip=request_ctx.client_ip,
        login_user_agent=request_ctx.user_agent,
        login_device_id=request_ctx.device_id,
        login_app_version=request_ctx.app_version,
    )
    session.add(auth_session)
    await session.flush()
    await session.refresh(auth_session)

    actor = AuditActor(
        actor_type=ActorType.USER,
        user_id=user.id,
        email=user.email,
        role=user.role,
        auth_method=AUTH_METHOD_PASSWORD,
        session=SessionInfo.from_row(auth_session),
        token_issued_at=issued_at,
    )
    login_event_id = await get_audit_recorder(request).record(
        session,
        audit_entry(
            request,
            action=AuditAction.LOGIN_SUCCEEDED,
            actor=actor,
            summary=f"{user.email} signed in (new session)",
            target_type="AUTH_SESSION",
            target_id=str(session_id),
            parameters={"auth_method": AUTH_METHOD_PASSWORD},
            after_state={
                "session_id": str(session_id),
                "expires_at": expires_at,
                "role": user.role,
            },
            related={"session_id": str(session_id)},
        ),
    )
    auth_session.login_audit_event_id = login_event_id
    await session.commit()

    ibkr_acc_str = user.account.ibkr_account if user.account else None

    return TokenResponse(
        access_token=token,
        token_type="bearer",
        user=UserResponse(
            id=user.id,
            email=user.email,
            role=user.role,
            is_active=user.is_active,
            ibkr_account_id=user.ibkr_account_id,
            ibkr_account=ibkr_acc_str,
        ),
    )


@router.post("/logout", response_model=LogoutResponse)
async def logout(
    request: Request,
    current_user: Annotated[UserModel, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> LogoutResponse:
    """End the caller's server-side session; its tokens stop being accepted."""
    principal = principal_from_request(request)
    session_info = principal.session if principal else None
    ended = False
    if session_info is not None:
        res = await session.execute(
            update(AuthSessionModel)
            .where(
                AuthSessionModel.id == session_info.session_id,
                AuthSessionModel.ended_at.is_(None),
            )
            .values(ended_at=func.clock_timestamp(), end_reason="LOGOUT")
        )
        ended = bool(getattr(res, "rowcount", 0))

    actor = actor_for_user(request, current_user)
    await get_audit_recorder(request).record(
        session,
        audit_entry(
            request,
            action=AuditAction.LOGOUT,
            actor=actor,
            summary=f"{current_user.email} signed out",
            result=AuditResult.SUCCEEDED,
            result_reason=None
            if session_info is not None
            else "Token has no server-side session; it remains valid until expiry.",
            target_type="AUTH_SESSION",
            target_id=str(session_info.session_id) if session_info else None,
            after_state={"session_ended": ended},
            related={"session_id": str(session_info.session_id)} if session_info else {},
        ),
    )
    await session.commit()
    return LogoutResponse(session_ended=ended)


@router.post("/sse-token", response_model=SseTokenResponse)
async def get_sse_token(
    request: Request,
    current_user: Annotated[UserModel, Depends(require_authenticated_user)],
) -> SseTokenResponse:
    """Issue a short-lived (5 minute) purpose-specific token for SSE streaming."""
    principal = principal_from_request(request)
    session_id = (
        str(principal.session.session_id) if principal and principal.session else None
    )
    sse_token = create_sse_token(
        user_id=current_user.id, expires_minutes=5, session_id=session_id
    )
    return SseTokenResponse(
        sse_token=sse_token,
        token_type="bearer",
        expires_in_seconds=300,
    )


@router.get("/me", response_model=UserResponse)
async def me(
    current_user: Annotated[UserModel, Depends(get_current_user)]
) -> UserResponse:
    """Get current authenticated user info."""
    ibkr_acc_str = current_user.account.ibkr_account if current_user.account else None
    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        role=current_user.role,
        is_active=current_user.is_active,
        ibkr_account_id=current_user.ibkr_account_id,
        ibkr_account=ibkr_acc_str,
    )
