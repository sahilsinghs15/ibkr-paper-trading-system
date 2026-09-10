"""Integration tests for GET /api/v1/broker/executions."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_db_session
from app.broker.ibkr.executions import BrokerExecutionLine
from app.core.security import create_access_token, get_password_hash
from app.db.models.account import AccountModel
from app.db.models.user import UserModel
from app.main import app
from app.schemas.broker_execution_schemas import BrokerExecutionsResponse


@pytest.mark.asyncio
async def test_broker_executions_api(session_factory) -> None:
    app.dependency_overrides.clear()

    suffix = uuid.uuid4().hex[:6]
    acc_a_code = f"DUA{suffix.upper()}"
    acc_b_code = f"DUB{suffix.upper()}"

    async with session_factory() as session:
        acc_a = AccountModel(name=f"Account A {suffix}", ibkr_account=acc_a_code, total_margin=100000, enabled=True)
        acc_b = AccountModel(name=f"Account B {suffix}", ibkr_account=acc_b_code, total_margin=200000, enabled=True)
        session.add_all([acc_a, acc_b])
        await session.commit()
        await session.refresh(acc_a)
        await session.refresh(acc_b)

        user_a = UserModel(
            email=f"user_a_{suffix}@example.com",
            password_hash=get_password_hash("Pass123!"),
            role="user",
            is_active=True,
            ibkr_account_id=acc_a.id,
        )
        user_b = UserModel(
            email=f"user_b_{suffix}@example.com",
            password_hash=get_password_hash("Pass123!"),
            role="user",
            is_active=True,
            ibkr_account_id=acc_b.id,
        )
        admin_user = UserModel(
            email=f"admin_{suffix}@example.com",
            password_hash=get_password_hash("Pass123!"),
            role="admin",
            is_active=True,
            ibkr_account_id=None,
        )
        session.add_all([user_a, user_b, admin_user])
        await session.commit()
        await session.refresh(user_a)
        await session.refresh(user_b)
        await session.refresh(admin_user)

        token_a = create_access_token({"sub": str(user_a.id), "role": "user", "email": user_a.email})
        token_b = create_access_token({"sub": str(user_b.id), "role": "user", "email": user_b.email})
        token_admin = create_access_token({"sub": str(admin_user.id), "role": "admin", "email": admin_user.email})

    async def _override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = _override_get_db

    # Mock TWSClient on app.state.client
    mock_client = MagicMock()
    app.state.client = mock_client

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1. Missing ibkr_account parameter -> 422
            resp = await client.get("/api/v1/broker/executions", headers={"Authorization": f"Bearer {token_a}"})
            assert resp.status_code == 422

            # 2. Unauthorized account access: User A trying to access Account B -> 403
            resp = await client.get(
                "/api/v1/broker/executions",
                params={"ibkr_account": acc_b_code},
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert resp.status_code == 403
            assert "Forbidden" in resp.json()["detail"]

            # User B trying to access Account A -> 403
            resp_b = await client.get(
                "/api/v1/broker/executions",
                params={"ibkr_account": acc_a_code},
                headers={"Authorization": f"Bearer {token_b}"},
            )
            assert resp_b.status_code == 403
            assert "Forbidden" in resp_b.json()["detail"]

            # 3. Disconnected Gateway -> 503
            mock_client.is_connected.return_value = False
            resp = await client.get(
                "/api/v1/broker/executions",
                params={"ibkr_account": acc_a_code},
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert resp.status_code == 503
            assert resp.json()["detail"] == "TWS gateway is down."

            # 4. Connected Gateway returning executions -> 200
            mock_client.is_connected.return_value = True
            sample_line = BrokerExecutionLine(
                exec_id="exec.test.101",
                executed_at="2026-09-10T10:15:30+00:00",
                ibkr_account=acc_a_code,
                symbol="AAPL",
                sec_type="STK",
                currency="USD",
                exchange="SMART",
                con_id=11111,
                side="BUY",
                quantity=50.0,
                price=150.0,
                cum_qty=50.0,
                avg_price=150.0,
                broker_order_id=9001,
                perm_id=12345678,
                client_id=0,
                commission=1.25,
                commission_currency="USD",
                realized_pnl=15.0,
            )
            foreign_line = BrokerExecutionLine(
                exec_id="exec.test.999",
                executed_at="2026-09-10T10:16:30+00:00",
                ibkr_account="DU_OTHER",
                symbol="MSFT",
                sec_type="STK",
                currency="USD",
                exchange="SMART",
                con_id=22222,
                side="SELL",
                quantity=10.0,
                price=300.0,
                cum_qty=10.0,
                avg_price=300.0,
                broker_order_id=9002,
                perm_id=87654321,
                client_id=0,
            )

            mock_client.request_executions_async = AsyncMock(return_value=([sample_line, foreign_line], False))

            resp = await client.get(
                "/api/v1/broker/executions",
                params={"ibkr_account": acc_a_code},
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert resp.status_code == 200
            data = resp.json()
            payload = BrokerExecutionsResponse.model_validate(data)

            assert payload.ibkr_account == acc_a_code
            assert payload.window == "since_midnight"
            assert payload.timed_out is False
            # Defense-in-depth: foreign_line with "DU_OTHER" dropped!
            assert len(payload.executions) == 1
            ex = payload.executions[0]
            assert ex.exec_id == "exec.test.101"
            assert ex.symbol == "AAPL"
            assert ex.side == "BUY"
            assert ex.quantity == 50.0
            assert ex.price == 150.0
            assert ex.commission == 1.25
            assert ex.realized_pnl == 15.0

            # 5. Admin can access any account
            resp_admin = await client.get(
                "/api/v1/broker/executions",
                params={"ibkr_account": acc_a_code},
                headers={"Authorization": f"Bearer {token_admin}"},
            )
            assert resp_admin.status_code == 200

            resp_admin_b = await client.get(
                "/api/v1/broker/executions",
                params={"ibkr_account": acc_b_code},
                headers={"Authorization": f"Bearer {token_admin}"},
            )
            assert resp_admin_b.status_code == 200
    finally:
        app.dependency_overrides.clear()
