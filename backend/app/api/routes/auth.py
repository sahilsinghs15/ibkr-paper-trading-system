"""Authentication API routes."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_authenticated_user
from app.core.security import create_access_token, create_sse_token, verify_password
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


class SseTokenResponse(BaseModel):
    sse_token: str
    token_type: str = "bearer"
    expires_in_seconds: int = 300


@router.post("/login", response_model=TokenResponse)
async def login(
    req: LoginRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> TokenResponse:
    """Authenticate user with email and password and issue JWT token."""
    email_clean = req.email.strip().lower()
    result = await session.execute(
        select(UserModel).where(UserModel.email == email_clean)
    )
    user = result.scalar_one_or_none()

    if user is None or not verify_password(req.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token(
        data={
            "sub": str(user.id),
            "role": user.role,
            "email": user.email,
        }
    )

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


@router.post("/sse-token", response_model=SseTokenResponse)
async def get_sse_token(
    current_user: Annotated[UserModel, Depends(require_authenticated_user)]
) -> SseTokenResponse:
    """Issue a short-lived (5 minute) purpose-specific token for SSE streaming."""
    sse_token = create_sse_token(user_id=current_user.id, expires_minutes=5)
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
