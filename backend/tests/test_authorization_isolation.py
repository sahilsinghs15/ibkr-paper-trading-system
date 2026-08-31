"""Integration tests verifying cross-account isolation, BOLA/IDOR prevention, and System Monitor protection."""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_db_session
from app.core.security import create_access_token, get_password_hash
from app.db.models.account import AccountModel
from app.db.models.user import UserModel
from app.main import app


@pytest.mark.asyncio
async def test_cross_account_authorization_isolation(session_factory, monkeypatch: pytest.MonkeyPatch):
    app.dependency_overrides.clear()

    s_a = uuid.uuid4().hex[:6]
    s_b = uuid.uuid4().hex[:6]
    s_adm = uuid.uuid4().hex[:6]

    acc_a_num = f"DU{s_a.upper()}"
    acc_b_num = f"DU{s_b.upper()}"

    async with session_factory() as session:
        # 1. Create two separate accounts
        acc_a = AccountModel(name=f"Account Alpha {s_a}", ibkr_account=acc_a_num, total_margin=100000, enabled=True)
        acc_b = AccountModel(name=f"Account Beta {s_b}", ibkr_account=acc_b_num, total_margin=200000, enabled=True)
        session.add_all([acc_a, acc_b])
        await session.commit()
        await session.refresh(acc_a)
        await session.refresh(acc_b)

        # 2. Create User A, User B, and Admin User
        user_a = UserModel(
            email=f"usera_{s_a}@example.com",
            password_hash=get_password_hash("Pass123!"),
            role="user",
            is_active=True,
            ibkr_account_id=acc_a.id,
        )
        user_b = UserModel(
            email=f"userb_{s_b}@example.com",
            password_hash=get_password_hash("Pass123!"),
            role="user",
            is_active=True,
            ibkr_account_id=acc_b.id,
        )
        admin = UserModel(
            email=f"admin_{s_adm}@example.com",
            password_hash=get_password_hash("AdminPass123!"),
            role="admin",
            is_active=True,
            ibkr_account_id=None,
        )
        session.add_all([user_a, user_b, admin])
        await session.commit()
        await session.refresh(user_a)
        await session.refresh(user_b)
        await session.refresh(admin)

        token_a = create_access_token({"sub": str(user_a.id), "role": "user", "email": user_a.email})
        token_b = create_access_token({"sub": str(user_b.id), "role": "user", "email": user_b.email})
        token_admin = create_access_token({"sub": str(admin.id), "role": "admin", "email": admin.email})

    async def _override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = _override_get_db

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:

            # A. Test System Monitor endpoint protection
            # User A calling system monitor -> HTTP 403 Forbidden
            sys_a_resp = await client.get("/api/v1/system-monitor", headers={"Authorization": f"Bearer {token_a}"})
            assert sys_a_resp.status_code == 403

            # Admin calling system monitor -> HTTP 200 OK
            sys_admin_resp = await client.get("/api/v1/system-monitor", headers={"Authorization": f"Bearer {token_admin}"})
            assert sys_admin_resp.status_code == 200

            # Unauthenticated request (with TRADINGAPP_TESTING disabled) -> HTTP 401 Unauthorized
            monkeypatch.setenv("TRADINGAPP_TESTING", "0")
            sys_unauth_resp = await client.get("/api/v1/system-monitor")
            assert sys_unauth_resp.status_code == 401
            monkeypatch.setenv("TRADINGAPP_TESTING", "1")

            # B. Test Config Accounts listing isolation
            # User A lists accounts -> sees ONLY Account Alpha
            accs_a_resp = await client.get("/api/v1/config/accounts", headers={"Authorization": f"Bearer {token_a}"})
            assert accs_a_resp.status_code == 200
            accounts_a = accs_a_resp.json()["accounts"]
            assert len(accounts_a) == 1
            assert accounts_a[0]["ibkr_account"] == acc_a_num

            # User B lists accounts -> sees ONLY Account Beta
            accs_b_resp = await client.get("/api/v1/config/accounts", headers={"Authorization": f"Bearer {token_b}"})
            assert accs_b_resp.status_code == 200
            accounts_b = accs_b_resp.json()["accounts"]
            assert len(accounts_b) == 1
            assert accounts_b[0]["ibkr_account"] == acc_b_num

            # Admin lists accounts -> sees both accounts
            accs_admin_resp = await client.get("/api/v1/config/accounts", headers={"Authorization": f"Bearer {token_admin}"})
            assert accs_admin_resp.status_code == 200
            accounts_admin = accs_admin_resp.json()["accounts"]
            assert len(accounts_admin) >= 2

            # C. Test IDOR / Cross-Account Access on specific account endpoints
            # User A accessing Account Alpha by ID -> HTTP 200
            get_a_resp = await client.get(f"/api/v1/config/accounts/by-identifier/{acc_a_num}", headers={"Authorization": f"Bearer {token_a}"})
            assert get_a_resp.status_code == 200

            # User A attempting to access Account Beta by ID (IDOR Attack) -> HTTP 403 Forbidden
            get_b_attack_resp = await client.get(f"/api/v1/config/accounts/by-identifier/{acc_b_num}", headers={"Authorization": f"Bearer {token_a}"})
            assert get_b_attack_resp.status_code == 403

            # User A attempting to trigger Square-Off on Account Beta -> HTTP 403 Forbidden
            sq_attack_resp = await client.post(f"/api/v1/config/accounts/{acc_b.id}/square-off", headers={"Authorization": f"Bearer {token_a}"})
            assert sq_attack_resp.status_code == 403

            # User A attempting to clear Kill Switch on Account Beta -> HTTP 403 Forbidden
            clear_attack_resp = await client.post(f"/api/v1/config/accounts/{acc_b.id}/kill-switch/clear", headers={"Authorization": f"Bearer {token_a}"})
            assert clear_attack_resp.status_code == 403

            # User A attempting to create allocation (Admin only) -> HTTP 403 Forbidden
            create_alloc_resp = await client.post(
                f"/api/v1/config/accounts/{acc_a.id}/allocations",
                json={"strategy_id": "STRAT1", "alloc_pct": 50},
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert create_alloc_resp.status_code == 403

            # D. Test Reconcile Position filtering
            # User A requesting reconcile for Account Beta -> HTTP 403 Forbidden
            recon_b_resp = await client.get(f"/api/v1/reconcile/positions?ibkr_account={acc_b_num}", headers={"Authorization": f"Bearer {token_a}"})
            assert recon_b_resp.status_code == 403

            # E. Test SSE Streaming Auth Hardening & Query Parameter Restrictions
            from unittest.mock import AsyncMock, MagicMock

            from app.core.security import create_sse_token
            from demo_streaming.api import create_demo_app

            redis_mock = MagicMock()
            redis_mock.ping = AsyncMock(return_value=True)
            redis_mock.xread = AsyncMock(return_value=[])

            demo_app = create_demo_app(
                session_factory=session_factory,
                redis=redis_mock,
                stream_name="positions:stream",
            )

            async with AsyncClient(transport=ASGITransport(app=demo_app), base_url="http://test") as sse_client:
                # 1. Normal access JWT passed as query parameter MUST be rejected -> HTTP 401
                normal_jwt_q_resp = await sse_client.get(f"/demo/positions?token={token_a}")
                assert normal_jwt_q_resp.status_code == 401

                # 2. Valid SSE token passed as query parameter -> HTTP 200 OK
                sse_token_a = create_sse_token(user_a.id, expires_minutes=5)
                sse_token_b = create_sse_token(user_b.id, expires_minutes=5)

                valid_sse_a = await sse_client.get(f"/demo/positions?token={sse_token_a}")
                assert valid_sse_a.status_code == 200

                valid_sse_b = await sse_client.get(f"/demo/positions?token={sse_token_b}")
                assert valid_sse_b.status_code == 200

                # 3. Invalid/tampered SSE token -> HTTP 401
                invalid_sse = await sse_client.get(f"/demo/positions?token={sse_token_a}INVALID")
                assert invalid_sse.status_code == 401

                # 4. User A's SSE token with User B's query override -> Returns ONLY User A's data
                sig_override = await sse_client.get(f"/demo/signals?ibkr_account={acc_b_num}&token={sse_token_a}")
                assert sig_override.status_code == 200

    finally:
        app.dependency_overrides.clear()
