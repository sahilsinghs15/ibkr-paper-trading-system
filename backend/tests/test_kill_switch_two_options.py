from uuid import uuid4
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import AccountModel, UserModel
from app.core.security import create_access_token
from app.db.session import get_db_session
from app.main import app
from app.services.kill_switch import is_account_kill_switch_active, clear_account_kill_switch


@pytest.fixture
async def async_client(session_factory: async_sessionmaker[AsyncSession]):
    async def _override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = _override_get_db
    app.state.session_factory = session_factory

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_square_off_engine_positions_endpoint(
    session_factory: async_sessionmaker[AsyncSession],
    async_client: AsyncClient,
) -> None:
    """Test Option 1: Close All Engine Positions."""
    tag = uuid4().hex[:6]
    async with session_factory() as session, session.begin():
        account = AccountModel(
            name=f"Test Engine Account {tag}",
            ibkr_account=f"DU{tag[:6]}1",
            total_margin=100000,
            enabled=True,
        )
        user = UserModel(
            email=f"operator_engine_{tag}@test.com",
            password_hash="hashed",
            role="admin",
            is_active=True,
        )
        session.add_all([account, user])
        await session.flush()
        account_id = account.id
        user_id = user.id

    token = create_access_token(data={"sub": str(user_id)})
    headers = {"Authorization": f"Bearer {token}"}

    response = await async_client.post(
        f"/api/v1/config/accounts/{account_id}/square-off?scope=engine",
        headers=headers,
    )
    assert response.status_code == 202
    data = response.json()
    assert data["account_id"] == account_id
    assert data["ibkr_account"] == account.ibkr_account
    assert data["scope"] == "ENGINE_POSITION_FLATTEN"
    assert is_account_kill_switch_active(account_id)

    # Cleanup kill switch cache
    await clear_account_kill_switch(session_factory, account_id)


@pytest.mark.asyncio
async def test_square_off_account_positions_endpoint(
    session_factory: async_sessionmaker[AsyncSession],
    async_client: AsyncClient,
) -> None:
    """Test Option 2: Close All Positions of Account (uses run_flatten_gateway_positions)."""
    tag = uuid4().hex[:6]
    async with session_factory() as session, session.begin():
        account = AccountModel(
            name=f"Test Account Flatten {tag}",
            ibkr_account=f"DU{tag[:6]}2",
            total_margin=100000,
            enabled=True,
        )
        user = UserModel(
            email=f"operator_account_{tag}@test.com",
            password_hash="hashed",
            role="admin",
            is_active=True,
        )
        session.add_all([account, user])
        await session.flush()
        account_id = account.id
        user_id = user.id

    token = create_access_token(data={"sub": str(user_id)})
    headers = {"Authorization": f"Bearer {token}"}

    mock_run_flatten = MagicMock(return_value={
        "success": True,
        "submitted": 3,
        "filled": 3,
        "rejected": 0,
        "pending": 0,
        "positions_found": 3,
        "error": None,
    })

    with patch("scripts.oms.flatten_gateway_positions.run_flatten_gateway_positions", mock_run_flatten):
        response = await async_client.post(
            f"/api/v1/config/accounts/{account_id}/square-off-account",
            headers=headers,
        )

    assert response.status_code == 202
    data = response.json()
    assert data["account_id"] == account_id
    assert data["ibkr_account"] == account.ibkr_account
    assert data["squared_off_count"] == 3
    assert data["scope"] == "ACCOUNT_POSITION_FLATTEN"
    assert data["status"] == "COMPLETE"
    assert is_account_kill_switch_active(account_id)

    # Verify run_flatten_gateway_positions was called with the exact target account string
    mock_run_flatten.assert_called_once()
    _, kwargs = mock_run_flatten.call_args
    assert kwargs["account"] == account.ibkr_account
    assert kwargs["apply"] is True

    # Cleanup kill switch cache
    await clear_account_kill_switch(session_factory, account_id)
