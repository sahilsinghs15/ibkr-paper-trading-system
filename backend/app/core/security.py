"""Security helper functions for password hashing and JWT token creation/verification."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain password against its bcrypt hash."""
    try:
        pwd_bytes = plain_password.encode("utf-8")
        if len(pwd_bytes) > 72:
            pwd_bytes = pwd_bytes[:72]
        return bcrypt.checkpw(pwd_bytes, hashed_password.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        logger.warning("Password verification error: %s", exc)
        return False


def get_password_hash(password: str) -> str:
    """Generate bcrypt hash for a plain password."""
    pwd_bytes = password.encode("utf-8")
    if len(pwd_bytes) > 72:
        pwd_bytes = pwd_bytes[:72]
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(pwd_bytes, salt).decode("utf-8")


def create_access_token(
    data: dict, expires_delta: timedelta | None = None
) -> str:
    """Create a signed JWT access token."""
    settings = get_settings()
    to_encode = data.copy()
    now = datetime.now(UTC)
    if expires_delta:
        expire = now + expires_delta
    else:
        expire = now + timedelta(minutes=settings.jwt_access_token_expire_minutes)
    
    to_encode.update({
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
        "type": "access",
    })
    
    encoded_jwt = jwt.encode(
        to_encode,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    return encoded_jwt


def decode_access_token(token: str) -> dict:
    """Decode and validate a JWT access token.
    
    Raises:
        jwt.PyJWTError: If signature invalid, expired, or malformed.
    """
    settings = get_settings()
    payload = jwt.decode(
        token,
        settings.jwt_secret_key,
        algorithms=[settings.jwt_algorithm],
    )
    if payload.get("type") != "access":
        raise jwt.InvalidTokenError("Invalid token type")
    return payload


def create_sse_token(user_id: int, expires_minutes: int = 5) -> str:
    """Create a short-lived, purpose-specific JWT token for SSE streaming."""
    settings = get_settings()
    now = datetime.now(UTC)
    expire = now + timedelta(minutes=expires_minutes)
    
    payload = {
        "sub": str(user_id),
        "type": "sse",
        "purpose": "sse",
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
    }
    
    return jwt.encode(
        payload,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


def decode_sse_token(token: str) -> dict:
    """Decode and validate an SSE-specific JWT token.
    
    Raises:
        jwt.PyJWTError: If signature invalid, expired, malformed, or not an SSE token.
    """
    settings = get_settings()
    payload = jwt.decode(
        token,
        settings.jwt_secret_key,
        algorithms=[settings.jwt_algorithm],
    )
    if payload.get("type") != "sse" or payload.get("purpose") != "sse":
        raise jwt.InvalidTokenError("Invalid token purpose")
    return payload
