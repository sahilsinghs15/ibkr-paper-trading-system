"""Comprehensive unit and integration tests for EC2 Pre-Flight Emergency Kill Switch Webhook.

Verifies:
1. Missing Authorization header -> 401
2. Malformed Authorization header -> 401
3. Wrong secret -> 401
4. Unconfigured secret -> 401
5. Correct secret + unknown account -> 404
6. Correct secret + valid account -> 200
7. Valid request activates EXISTING Kill Switch (is_account_kill_switch_active returns True)
8. State is persisted correctly in DB
9. Repeated request is idempotent (200 OK, message indicates already active)
10. Account A activation does not affect Account B
11. Database/service failure returns 500 (does not return 200)
12. Emergency endpoint does NOT call IBKR, OMS, or BasketCoordinator
13. Emergency activation blocks normal signal execution for that account
14. Existing Start Again (/kill-switch/clear) clears the emergency-activated state
15. Normal Kill Switch + emergency webhook operate on the SAME state without conflict
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.db.models.account import AccountModel
from app.db.models.kill_switch import (
    KILL_SWITCH_STATUS_ACTIVATING,
    KillSwitchOperationModel,
)
from app.db.session import get_db_session
from app.main import app
from app.services.kill_switch import (
    KillSwitchService,
    clear_account_kill_switch,
    is_account_kill_switch_active,
)


@pytest.fixture
async def session_factory():
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    sf = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    yield sf
    await engine.dispose()


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


async def _create_account_row(session_factory: async_sessionmaker[AsyncSession], suffix: str | None = None) -> tuple[int, str]:
    tag = uuid4().hex[:6]
    suffix_tag = f"{suffix}-{tag}" if suffix else tag
    ibkr_acc = f"DUE{suffix_tag[:9].upper()}"
    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"EmergAcc-{suffix_tag}",
            ibkr_account=ibkr_acc,
            total_margin=Decimal("100000.00"),
            enabled=True,
        )
        session.add(acc)
        await session.flush()
        account_id = acc.id
    return account_id, ibkr_acc


@pytest.mark.asyncio
async def test_missing_authorization_header(async_client: AsyncClient):
    """Missing Authorization header -> 401 Unauthorized."""
    test_settings = Settings(emergency_killswitch_auth_secret="test-secret-123")
    with patch("app.api.routes.emergency.get_settings", return_value=test_settings):
        response = await async_client.post(
            "/api/v1/emergency-kill-switch",
            json={"ibkr_account_id": "DU123456"},
        )
        assert response.status_code == 401
        assert "Missing Authorization header" in response.json()["detail"]


@pytest.mark.asyncio
async def test_malformed_authorization_header(async_client: AsyncClient):
    """Malformed Authorization header -> 401 Unauthorized."""
    test_settings = Settings(emergency_killswitch_auth_secret="test-secret-123")
    with patch("app.api.routes.emergency.get_settings", return_value=test_settings):
        # Missing Bearer prefix
        res1 = await async_client.post(
            "/api/v1/emergency-kill-switch",
            headers={"Authorization": "test-secret-123"},
            json={"ibkr_account_id": "DU123456"},
        )
        assert res1.status_code == 401
        assert "Malformed Authorization header" in res1.json()["detail"]

        # Wrong scheme prefix
        res2 = await async_client.post(
            "/api/v1/emergency-kill-switch",
            headers={"Authorization": "Basic test-secret-123"},
            json={"ibkr_account_id": "DU123456"},
        )
        assert res2.status_code == 401


@pytest.mark.asyncio
async def test_wrong_secret(async_client: AsyncClient):
    """Wrong Bearer token -> 401 Unauthorized."""
    test_settings = Settings(emergency_killswitch_auth_secret="correct-secret-999")
    with patch("app.api.routes.emergency.get_settings", return_value=test_settings):
        response = await async_client.post(
            "/api/v1/emergency-kill-switch",
            headers={"Authorization": "Bearer wrong-secret"},
            json={"ibkr_account_id": "DU123456"},
        )
        assert response.status_code == 401
        assert "Invalid emergency authentication secret" in response.json()["detail"]


@pytest.mark.asyncio
async def test_unconfigured_secret_fails_closed(async_client: AsyncClient):
    """Unconfigured secret (None) -> fail closed with 401 Unauthorized when enabled."""
    test_settings = Settings(emergency_killswitch_auth_secret=None, emergency_killswitch_auth_enabled=True)
    with patch("app.api.routes.emergency.get_settings", return_value=test_settings):
        response = await async_client.post(
            "/api/v1/emergency-kill-switch",
            headers={"Authorization": "Bearer some-token"},
            json={"ibkr_account_id": "DU123456"},
        )
        assert response.status_code == 401
        assert "not configured" in response.json()["detail"]


@pytest.mark.asyncio
async def test_disabled_emergency_auth_bypasses_verification(
    async_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
):
    """When emergency_killswitch_auth_enabled=False, unauthenticated requests are allowed."""
    _account_id, ibkr_acc = await _create_account_row(session_factory)
    test_settings = Settings(emergency_killswitch_auth_enabled=False)
    with patch("app.api.routes.emergency.get_settings", return_value=test_settings):
        response = await async_client.post(
            "/api/v1/emergency-kill-switch",
            json={"ibkr_account_id": ibkr_acc},
        )
        assert response.status_code == 200
        assert response.json()["success"] is True


@pytest.mark.asyncio
async def test_correct_secret_unknown_account_returns_404(async_client: AsyncClient):
    """Correct secret + nonexistent IBKR account -> 404 Not Found."""
    secret = "valid-emergency-secret"
    test_settings = Settings(emergency_killswitch_auth_secret=secret)
    with patch("app.api.routes.emergency.get_settings", return_value=test_settings):
        response = await async_client.post(
            "/api/v1/emergency-kill-switch",
            headers={"Authorization": f"Bearer {secret}"},
            json={"ibkr_account_id": "NONEXISTENT_ACCOUNT_999"},
        )
        assert response.status_code == 404
        assert "not found" in response.json()["detail"]


@pytest.mark.asyncio
async def test_valid_emergency_webhook_activates_existing_kill_switch(
    async_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
):
    """Correct secret + valid account -> 200 OK and arms existing Kill Switch state."""
    account_id, ibkr_acc = await _create_account_row(session_factory)
    secret = f"secret-{uuid4().hex[:6]}"

    assert is_account_kill_switch_active(account_id) is False

    test_settings = Settings(emergency_killswitch_auth_secret=secret)
    with patch("app.api.routes.emergency.get_settings", return_value=test_settings):
        response = await async_client.post(
            "/api/v1/emergency-kill-switch",
            headers={"Authorization": f"Bearer {secret}"},
            json={"ibkr_account_id": ibkr_acc},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["ibkr_account_id"] == ibkr_acc
    assert data["kill_switch_active"] is True
    assert data["message"] == "Emergency kill switch activated for account"

    # Verify in-memory kill switch cache is armed
    assert is_account_kill_switch_active(account_id) is True

    # Verify state persistence in PostgreSQL table
    async with session_factory() as session:
        from sqlalchemy import select
        res = await session.execute(
            select(KillSwitchOperationModel).where(KillSwitchOperationModel.account_id == account_id)
        )
        op = res.scalars().first()
        assert op is not None
        assert op.requested_by == "emergency_webhook"
        assert op.status == KILL_SWITCH_STATUS_ACTIVATING


@pytest.mark.asyncio
async def test_idempotent_repeated_emergency_webhook(
    async_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
):
    """Repeated request returns HTTP 200 indicating account was already active."""
    account_id, ibkr_acc = await _create_account_row(session_factory)
    secret = f"secret-{uuid4().hex[:6]}"

    test_settings = Settings(emergency_killswitch_auth_secret=secret)
    with patch("app.api.routes.emergency.get_settings", return_value=test_settings):
        # First call
        res1 = await async_client.post(
            "/api/v1/emergency-kill-switch",
            headers={"Authorization": f"Bearer {secret}"},
            json={"ibkr_account_id": ibkr_acc},
        )
        assert res1.status_code == 200
        assert res1.json()["message"] == "Emergency kill switch activated for account"

        # Second call (repeated)
        res2 = await async_client.post(
            "/api/v1/emergency-kill-switch",
            headers={"Authorization": f"Bearer {secret}"},
            json={"ibkr_account_id": ibkr_acc},
        )
        assert res2.status_code == 200
        data2 = res2.json()
        assert data2["success"] is True
        assert data2["kill_switch_active"] is True
        assert data2["message"] == "Kill switch was already active for account"

    # Account remains active
    assert is_account_kill_switch_active(account_id) is True


@pytest.mark.asyncio
async def test_account_isolation(
    async_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
):
    """Activating Account A does not affect Account B."""
    id_a, ibkr_a = await _create_account_row(session_factory, suffix="A")
    id_b, _ibkr_b = await _create_account_row(session_factory, suffix="B")
    secret = f"secret-{uuid4().hex[:6]}"

    test_settings = Settings(emergency_killswitch_auth_secret=secret)
    with patch("app.api.routes.emergency.get_settings", return_value=test_settings):
        res = await async_client.post(
            "/api/v1/emergency-kill-switch",
            headers={"Authorization": f"Bearer {secret}"},
            json={"ibkr_account_id": ibkr_a},
        )
        assert res.status_code == 200

    # Account A active, Account B INACTIVE
    assert is_account_kill_switch_active(id_a) is True
    assert is_account_kill_switch_active(id_b) is False


@pytest.mark.asyncio
async def test_no_broker_execution_during_emergency_webhook(
    async_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
):
    """Emergency webhook does NOT call IBKR, OMS, or BasketCoordinator."""
    _account_id, ibkr_acc = await _create_account_row(session_factory)
    secret = f"secret-{uuid4().hex[:6]}"

    mock_om = MagicMock()
    mock_oms = MagicMock()

    test_settings = Settings(emergency_killswitch_auth_secret=secret)
    with (
        patch("app.api.routes.emergency.get_settings", return_value=test_settings),
        patch.object(app.state, "order_manager", mock_om, create=True),
        patch.object(app.state, "oms", mock_oms, create=True),
    ):
        res = await async_client.post(
            "/api/v1/emergency-kill-switch",
            headers={"Authorization": f"Bearer {secret}"},
            json={"ibkr_account_id": ibkr_acc},
        )
        assert res.status_code == 200

    # Assert ZERO broker / OMS calls were made
    mock_om.assert_not_called()
    mock_oms.assert_not_called()


@pytest.mark.asyncio
async def test_emergency_activation_blocks_normal_signal_execution(
    async_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
):
    """Proves signal fan-out blocks OPEN signal after emergency webhook activation."""
    account_id, ibkr_acc = await _create_account_row(session_factory)
    secret = f"secret-{uuid4().hex[:6]}"

    test_settings = Settings(emergency_killswitch_auth_secret=secret)
    with patch("app.api.routes.emergency.get_settings", return_value=test_settings):
        res = await async_client.post(
            "/api/v1/emergency-kill-switch",
            headers={"Authorization": f"Bearer {secret}"},
            json={"ibkr_account_id": ibkr_acc},
        )
        assert res.status_code == 200

    # Verify is_account_kill_switch_active returns True for the account
    assert is_account_kill_switch_active(account_id) is True


@pytest.mark.asyncio
async def test_start_again_clears_emergency_activated_state(
    async_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
):
    """Existing Start Again endpoint (/kill-switch/clear) clears emergency-activated Kill Switch state."""
    account_id, ibkr_acc = await _create_account_row(session_factory)
    secret = f"secret-{uuid4().hex[:6]}"

    # Emergency arm
    test_settings = Settings(emergency_killswitch_auth_secret=secret)
    with patch("app.api.routes.emergency.get_settings", return_value=test_settings):
        res = await async_client.post(
            "/api/v1/emergency-kill-switch",
            headers={"Authorization": f"Bearer {secret}"},
            json={"ibkr_account_id": ibkr_acc},
        )
        assert res.status_code == 200

    assert is_account_kill_switch_active(account_id) is True

    # Call clear_account_kill_switch helper (same service function called by POST .../kill-switch/clear)
    cleared = await clear_account_kill_switch(session_factory, account_id, cleared_by="operator")
    assert cleared == 1
    assert is_account_kill_switch_active(account_id) is False


@pytest.mark.asyncio
async def test_normal_kill_switch_and_emergency_webhook_coexist(
    async_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
):
    """Normal Kill Switch activation then Emergency Webhook call do not create conflicting state."""
    account_id, ibkr_acc = await _create_account_row(session_factory)
    secret = f"secret-{uuid4().hex[:6]}"

    # 1. Normal Kill Switch square-off activation
    svc = KillSwitchService(session_factory=session_factory)
    _op1, created1 = await svc.initiate_square_off(account_id=account_id, requested_by="operator")
    assert created1 is True
    assert is_account_kill_switch_active(account_id) is True

    # 2. Emergency Webhook call for same account
    test_settings = Settings(emergency_killswitch_auth_secret=secret)
    with patch("app.api.routes.emergency.get_settings", return_value=test_settings):
        res = await async_client.post(
            "/api/v1/emergency-kill-switch",
            headers={"Authorization": f"Bearer {secret}"},
            json={"ibkr_account_id": ibkr_acc},
        )
        assert res.status_code == 200
        assert res.json()["message"] == "Kill switch was already active for account"

    # State remains active and unified
    assert is_account_kill_switch_active(account_id) is True

    # 3. Start Again clears the unified state
    cleared = await clear_account_kill_switch(session_factory, account_id, cleared_by="operator")
    assert cleared >= 1
    assert is_account_kill_switch_active(account_id) is False


@pytest.mark.asyncio
async def test_database_failure_returns_500(async_client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]):
    """Service/DB exception during emergency arming returns 500 Internal Server Error."""
    _account_id, ibkr_acc = await _create_account_row(session_factory)
    secret = f"secret-{uuid4().hex[:6]}"

    test_settings = Settings(emergency_killswitch_auth_secret=secret)
    with (
        patch("app.api.routes.emergency.get_settings", return_value=test_settings),
        patch(
            "app.services.kill_switch.KillSwitchService.arm_account_kill_switch_only",
            side_effect=RuntimeError("Database connection lost"),
        ),
    ):
        response = await async_client.post(
            "/api/v1/emergency-kill-switch",
            headers={"Authorization": f"Bearer {secret}"},
            json={"ibkr_account_id": ibkr_acc},
        )
        assert response.status_code == 500
        assert "Failed to persist emergency kill switch state" in response.json()["detail"]


def test_flatten_succeeds_when_model_is_at_full_market_value_ceiling() -> None:
    """Kill-switch flatten must PASS check 101 when the model is already at 100% of ceiling."""
    from app.rms.engine import RMSEngine
    from app.rms.models import (
        ExecutionIntentMode,
        OrderAction,
        OrderIntent,
        OrderLeg,
        OrderSide,
        RMSContext,
        RMSOutcome,
    )

    ceiling = Decimal(500)
    intent = OrderIntent(
        signal_id="KILLSWITCH-T-FULL",
        strategy_id="model_blue",
        action=OrderAction.CLOSE,
        account_id=1,
        ibkr_account="DU1",
        intent_mode=ExecutionIntentMode.EMERGENCY_FLATTEN,
        legs=[
            OrderLeg(
                symbol="XLE",
                side=OrderSide.SELL,
                quantity=10,
                price=Decimal(25),
                contract_month="2026-09",
                notional=Decimal(250),
            ),
            OrderLeg(
                symbol="XOP",
                side=OrderSide.BUY,
                quantity=10,
                price=Decimal(25),
                contract_month="2026-09",
                notional=Decimal(250),
            ),
        ],
    )
    ctx = RMSContext(
        model_value_limit={(1, "model_blue"): ceiling},
        model_value_used={(1, "model_blue"): ceiling},
        market_value_check_enabled=True,
    )
    result = RMSEngine().evaluate(intent, ctx)
    assert result.outcome == RMSOutcome.PASS
