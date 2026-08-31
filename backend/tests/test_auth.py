"""Unit and integration tests for JWT authentication and user provisioning."""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import (
    get_db_session,
)
from app.core.security import (
    create_access_token,
    create_sse_token,
    decode_access_token,
    decode_sse_token,
    get_password_hash,
    verify_password,
)
from app.db.models.account import AccountModel
from app.db.models.user import UserModel
from app.main import app


def test_password_hashing():
    raw_pwd = "SuperSecretPassword123!"
    hashed = get_password_hash(raw_pwd)
    assert hashed != raw_pwd
    assert verify_password(raw_pwd, hashed) is True
    assert verify_password("WrongPassword", hashed) is False


def test_jwt_token_lifecycle():
    data = {"sub": "42", "role": "admin", "email": "admin@example.com"}
    token = create_access_token(data)
    assert isinstance(token, str)

    payload = decode_access_token(token)
    assert payload["sub"] == "42"
    assert payload["role"] == "admin"
    assert payload["email"] == "admin@example.com"
    assert payload["type"] == "access"
    assert "exp" in payload


def test_sse_token_lifecycle():
    token = create_sse_token(user_id=42, expires_minutes=5)
    assert isinstance(token, str)

    payload = decode_sse_token(token)
    assert payload["sub"] == "42"
    assert payload["type"] == "sse"
    assert payload["purpose"] == "sse"

    # Verify that an SSE token cannot be decoded as an access token
    import jwt
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(token)


@pytest.mark.asyncio
async def test_auth_login_flow(session_factory, monkeypatch: pytest.MonkeyPatch):
    app.dependency_overrides.clear()

    suffix = uuid.uuid4().hex[:6]
    test_email = f"trader_{suffix}@example.com"
    test_ibkr_acc = f"U{suffix}"

    async with session_factory() as session:
        acc = AccountModel(
            name=f"Test Account {suffix}",
            ibkr_account=test_ibkr_acc,
            total_margin=100000,
            enabled=True,
        )
        session.add(acc)
        await session.commit()
        await session.refresh(acc)

        pwd_hash = get_password_hash("ValidPass123!")
        user = UserModel(
            email=test_email,
            password_hash=pwd_hash,
            role="user",
            is_active=True,
            ibkr_account_id=acc.id,
        )
        session.add(user)
        await session.commit()

    async def _override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = _override_get_db

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1. Unauthenticated request to /auth/sse-token -> 401
            monkeypatch.setenv("TRADINGAPP_TESTING", "0")
            unauth_sse = await client.post("/api/v1/auth/sse-token")
            assert unauth_sse.status_code == 401
            monkeypatch.setenv("TRADINGAPP_TESTING", "1")

            # 2. Login
            resp = await client.post(
                "/api/v1/auth/login",
                json={"email": test_email, "password": "ValidPass123!"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert "access_token" in body
            assert body["token_type"] == "bearer"
            assert body["user"]["email"] == test_email
            assert body["user"]["role"] == "user"
            assert body["user"]["ibkr_account"] == test_ibkr_acc

            token = body["access_token"]

            # 3. Authenticated request to /auth/me
            me_resp = await client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert me_resp.status_code == 200
            assert me_resp.json()["email"] == test_email

            # 4. Authenticated request to /auth/sse-token -> 200 returning short-lived sse_token
            sse_resp = await client.post(
                "/api/v1/auth/sse-token",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert sse_resp.status_code == 200
            sse_body = sse_resp.json()
            assert "sse_token" in sse_body
            assert sse_body["expires_in_seconds"] == 300
            sse_token = sse_body["sse_token"]

            # 5. Attempt to use SSE token on REST endpoint /auth/me -> 401 Unauthorized
            sse_me_resp = await client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {sse_token}"},
            )
            assert sse_me_resp.status_code == 401

            invalid_pwd_resp = await client.post(
                "/api/v1/auth/login",
                json={"email": test_email, "password": "WrongPassword"},
            )
            assert invalid_pwd_resp.status_code == 401

            no_user_resp = await client.post(
                "/api/v1/auth/login",
                json={"email": f"nonexistent_{suffix}@example.com", "password": "ValidPass123!"},
            )
            assert no_user_resp.status_code == 401

            reg_resp = await client.post(
                "/api/v1/auth/register",
                json={"email": f"new_{suffix}@example.com", "password": "Pass"},
            )
            assert reg_resp.status_code in (404, 405)
    finally:
        app.dependency_overrides.clear()
