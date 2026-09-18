"""FastAPI dependencies for JWT authentication and role-based authorization."""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.audit.context import PRINCIPAL_STATE_KEY, Principal, SessionInfo
from app.audit.security_events import record_session_token_rejected
from app.core.config import running_under_pytest
from app.core.security import decode_access_token
from app.db.models.audit import AuthSessionModel
from app.db.models.user import UserModel
from app.db.session import get_db_session
from app.oms.oms_service import OMSService
from app.services.order_manager import OrderManager

logger = logging.getLogger(__name__)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


def get_oms(request: Request) -> OMSService:
    """Retrieve the global OMSService instance from application state."""
    return request.app.state.oms


def get_order_manager(request: Request) -> OrderManager:
    """Retrieve the global OrderManager instance from application state."""
    return request.app.state.order_manager


async def get_token_from_request(
    request: Request,
    header_token: Annotated[str | None, Depends(oauth2_scheme)] = None,
    query_token: Annotated[str | None, Query(alias="token")] = None,
) -> str | None:
    """Extract JWT token from Authorization header or URL query parameter."""
    if header_token:
        return header_token
    if query_token:
        return query_token
    # Also check raw Authorization header if oauth2_scheme did not pick it up
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header[7:].strip()
    return None


async def get_current_user(
    request: Request,
    token: Annotated[str | None, Depends(get_token_from_request)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> UserModel:
    """Validate JWT access token and return active user from database.

    When the token carries a ``sid`` claim, the server-side auth session must
    exist and must not have ended (logout). The verified identity and session
    are attached to ``request.state`` for audit attribution; identity is never
    taken from request bodies or client headers.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if token is None:
        if os.environ.get("TRADINGAPP_TESTING") == "1" and running_under_pytest():
            mock_user = UserModel(
                id=999999,
                email="test_admin@example.com",
                password_hash="mock",
                role="admin",
                is_active=True,
                ibkr_account_id=None,
            )
            setattr(
                request.state,
                PRINCIPAL_STATE_KEY,
                Principal(
                    user_id=999999,
                    email="test_admin@example.com",
                    role="admin",
                    auth_method="pytest-bypass",
                ),
            )
            return mock_user
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = decode_access_token(token)
        user_id_str: str | None = payload.get("sub")
        if user_id_str is None:
            raise credentials_exception
        user_id = int(user_id_str)
        sid_raw = payload.get("sid")
        session_uuid = uuid.UUID(str(sid_raw)) if sid_raw is not None else None
    except (jwt.PyJWTError, ValueError) as exc:
        logger.warning("JWT validation failed: %s", exc)
        raise credentials_exception from exc

    auth_session: AuthSessionModel | None = None
    if session_uuid is None:
        result = await session.execute(
            select(UserModel).options(selectinload(UserModel.account)).where(UserModel.id == user_id)
        )
        user = result.scalar_one_or_none()
    else:
        joined = await session.execute(
            select(UserModel, AuthSessionModel)
            .options(selectinload(UserModel.account))
            .outerjoin(
                AuthSessionModel,
                and_(AuthSessionModel.id == session_uuid, AuthSessionModel.user_id == UserModel.id),
            )
            .where(UserModel.id == user_id)
        )
        row = joined.one_or_none()
        user, auth_session = (row[0], row[1]) if row is not None else (None, None)

    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User inactive or not found",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if session_uuid is not None and (auth_session is None or auth_session.ended_at is not None):
        reason = (
            "Session not found"
            if auth_session is None
            else f"Session ended ({auth_session.end_reason or 'ENDED'})"
        )
        await record_session_token_rejected(
            request,
            user_id=user.id,
            email=user.email,
            session_id=str(session_uuid),
            reason=reason,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has ended. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    iat = payload.get("iat")
    setattr(
        request.state,
        PRINCIPAL_STATE_KEY,
        Principal(
            user_id=user.id,
            email=user.email,
            role=user.role,
            auth_method=auth_session.auth_method if auth_session else "jwt-no-server-session",
            token_issued_at=datetime.fromtimestamp(iat, UTC) if isinstance(iat, int) else None,
            session=SessionInfo.from_row(auth_session) if auth_session else None,
        ),
    )
    return user


async def require_authenticated_user(
    current_user: Annotated[UserModel, Depends(get_current_user)]
) -> UserModel:
    """Dependency enforcing that the caller is an active authenticated user."""
    return current_user


async def require_admin(
    current_user: Annotated[UserModel, Depends(get_current_user)]
) -> UserModel:
    """Dependency enforcing that the caller has admin role."""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operation restricted to administrator role",
        )
    return current_user
