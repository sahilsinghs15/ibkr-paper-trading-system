"""Shared helpers for operator audit trail tests."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import get_password_hash
from app.db.models.account import AccountModel
from app.db.models.audit import AuditEventModel
from app.db.models.user import UserModel
from app.main import app

TEST_PASSWORD = "Corr3ct-Horse-Battery!"
_PASSWORD_HASH = get_password_hash(TEST_PASSWORD)

CHROME_WINDOWS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.6478.127 Safari/537.36"
)
FIREFOX_MAC_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5; rv:127.0) Gecko/20100101 Firefox/127.0"
)


def install_app_state(session_factory: async_sessionmaker[AsyncSession]) -> MagicMock:
    """Point the (lifespan-less) test app at the test DB and a stub order manager."""
    app.state.session_factory = session_factory
    order_manager = MagicMock()
    order_manager.reload_rms_limits = AsyncMock()
    order_manager.reload_execution_policy = AsyncMock()
    order_manager.reload_margin_settings = AsyncMock()
    app.state.order_manager = order_manager
    return order_manager


def client_for(ip: str = "203.0.113.10") -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, client=(ip, 51234)),
        base_url="http://test",
    )


async def create_user(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    role: str = "admin",
    account_id: int | None = None,
) -> UserModel:
    suffix = uuid.uuid4().hex[:10]
    async with session_factory() as s, s.begin():
        user = UserModel(
            email=f"audit-{role}-{suffix}@example.com",
            password_hash=_PASSWORD_HASH,
            role=role,
            is_active=True,
            ibkr_account_id=account_id,
        )
        s.add(user)
        await s.flush()
        await s.refresh(user)
    return user


async def create_account(session_factory: async_sessionmaker[AsyncSession]) -> AccountModel:
    suffix = uuid.uuid4().hex[:8].upper()
    # Explicit id: other suites insert explicit ids, so the sequence may lag.
    account_id = 50_000_000 + uuid.uuid4().int % 1_000_000_000
    async with session_factory() as s, s.begin():
        account = AccountModel(
            id=account_id,
            name=f"Audit {suffix}",
            ibkr_account=f"DUA{suffix}",
            total_margin=Decimal(100000),
            enabled=True,
            default_symbol_limit=Decimal(50000),
        )
        s.add(account)
        await s.flush()
        await s.refresh(account)
    return account


async def login(
    client: AsyncClient,
    user: UserModel,
    *,
    user_agent: str = CHROME_WINDOWS_UA,
    device_id: str | None = "dev-4f0c2a9e-1111-2222",
) -> str:
    headers = {"User-Agent": user_agent}
    if device_id:
        headers["X-Client-Device-Id"] = device_id
    res = await client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": TEST_PASSWORD},
        headers=headers,
    )
    assert res.status_code == 200, res.text
    return res.json()["access_token"]


def auth_headers(
    token: str,
    *,
    user_agent: str = CHROME_WINDOWS_UA,
    device_id: str | None = "dev-4f0c2a9e-1111-2222",
    **extra: str,
) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}", "User-Agent": user_agent, **extra}
    if device_id:
        headers["X-Client-Device-Id"] = device_id
    return headers


async def audit_rows(
    session_factory: async_sessionmaker[AsyncSession], **filters: Any
) -> list[AuditEventModel]:
    stmt = select(AuditEventModel)
    for key, value in filters.items():
        stmt = stmt.where(getattr(AuditEventModel, key) == value)
    stmt = stmt.order_by(AuditEventModel.id)
    async with session_factory() as s:
        return list((await s.execute(stmt)).scalars().all())


async def single_audit(
    session_factory: async_sessionmaker[AsyncSession], **filters: Any
) -> AuditEventModel:
    rows = await audit_rows(session_factory, **filters)
    assert len(rows) == 1, f"expected exactly one audit row for {filters}, got {len(rows)}"
    return rows[0]
