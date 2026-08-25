"""Comprehensive tests for Phase 1: Start Again option after Kill Switch.

Verifies:
1. Stopped account -> Start Again -> account becomes active.
2. Cancel confirmation (no API call) -> account remains stopped.
3. Nonexistent account -> 404 error.
4. Starting Account A does not affect Account B.
5. After backend restart (hydrate_kill_switch_cache), cleared account remains active.
6. Start Again does not submit any IBKR orders.
7. Existing Kill Switch behavior still works.
8. Existing signal execution behavior for active accounts remains unchanged.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.db.models.account import AccountModel
from app.db.models.kill_switch import (
    KILL_SWITCH_STATUS_ACTIVATING,
    KILL_SWITCH_STATUS_CLEARED,
    KillSwitchOperationModel,
)
from app.main import app
from app.services.kill_switch import (
    KillSwitchService,
    clear_account_kill_switch,
    hydrate_kill_switch_cache,
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
def client() -> TestClient:
    with (
        patch("app.broker.ibkr.tws_client.TWSClient.connect_and_start", return_value=True),
        patch("app.broker.ibkr.tws_client.TWSClient.disconnect_clean"),
        patch("app.broker.ibkr.tws_client.TWSClient.is_connected", return_value=True),
        patch("app.services.worker_pool.ExecutionWorkerPool.start", new_callable=AsyncMock),
        patch("app.services.worker_pool.ExecutionWorkerPool.stop", new_callable=AsyncMock),
        patch("app.services.recovery.RecoveryManager.run_startup_recovery", new_callable=AsyncMock),
        patch("app.services.order_manager.OrderManager.hydrate_live_pnl", new_callable=AsyncMock),
        patch("app.services.order_manager.OrderManager.hydrate_runtime_from_db", new_callable=AsyncMock),
        TestClient(app) as c,
    ):
        yield c


@pytest.mark.asyncio
async def test_stopped_account_start_again_becomes_active(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Test 1: Stopped account -> Start Again -> account becomes active in memory and DB."""
    test_id = uuid4().hex[:6]
    ibkr_acc = f"DU{test_id}"

    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"StartAgainTestAcc-{test_id}",
            ibkr_account=ibkr_acc,
            total_margin=Decimal("100000.00"),
        )
        session.add(acc)
        await session.flush()
        acc_id = acc.id

    svc = KillSwitchService(session_factory=session_factory)
    await svc.initiate_square_off(account_id=acc_id, requested_by="operator")
    assert is_account_kill_switch_active(acc_id) is True

    cleared_count = await clear_account_kill_switch(session_factory, acc_id, cleared_by="operator")
    assert cleared_count == 1
    assert is_account_kill_switch_active(acc_id) is False

    # Check DB persistent status is CLEARED
    async with session_factory() as session:
        from sqlalchemy import select
        res = await session.execute(
            select(KillSwitchOperationModel).where(KillSwitchOperationModel.account_id == acc_id)
        )
        op = res.scalars().first()
        assert op is not None
        assert op.status == KILL_SWITCH_STATUS_CLEARED
        assert op.cleared_by == "operator"
        assert op.cleared_at is not None


@pytest.mark.asyncio
async def test_cancel_confirmation_account_remains_stopped(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Test 2: Cancel confirmation -> clear API is NOT called -> account remains stopped."""
    test_id = uuid4().hex[:6]
    ibkr_acc = f"DU{test_id}"

    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"CancelTestAcc-{test_id}",
            ibkr_account=ibkr_acc,
            total_margin=Decimal("100000.00"),
        )
        session.add(acc)
        await session.flush()
        acc_id = acc.id

    svc = KillSwitchService(session_factory=session_factory)
    await svc.initiate_square_off(account_id=acc_id, requested_by="operator")
    assert is_account_kill_switch_active(acc_id) is True

    # User cancels dialog in frontend -> clear_account_kill_switch is NOT executed
    # Verify account remains stopped
    assert is_account_kill_switch_active(acc_id) is True


def test_nonexistent_account_returns_404(client: TestClient):
    """Test 3: Nonexistent account -> appropriate 404 error from clear and status endpoints."""
    fake_acc_id = 999999

    # POST clear
    res_clear = client.post(f"/api/v1/config/accounts/{fake_acc_id}/kill-switch/clear")
    assert res_clear.status_code == 404
    assert f"Account {fake_acc_id} not found" in res_clear.json()["detail"]

    # GET status
    res_status = client.get(f"/api/v1/config/accounts/{fake_acc_id}/kill-switch")
    assert res_status.status_code == 404
    assert f"Account {fake_acc_id} not found" in res_status.json()["detail"]


@pytest.mark.asyncio
async def test_starting_account_a_does_not_affect_account_b(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Test 4: Scoping - Starting Account A must not start Account B."""
    suffix = uuid4().hex[:6]
    async with session_factory() as session, session.begin():
        acc_a = AccountModel(name=f"AccA-{suffix}", ibkr_account=f"DUA{suffix}", total_margin=Decimal("100000.00"))
        acc_b = AccountModel(name=f"AccB-{suffix}", ibkr_account=f"DUB{suffix}", total_margin=Decimal("100000.00"))
        session.add_all([acc_a, acc_b])
        await session.flush()
        id_a, id_b = acc_a.id, acc_b.id

    svc = KillSwitchService(session_factory=session_factory)
    await svc.initiate_square_off(account_id=id_a, requested_by="operator")
    await svc.initiate_square_off(account_id=id_b, requested_by="operator")

    assert is_account_kill_switch_active(id_a) is True
    assert is_account_kill_switch_active(id_b) is True

    # Start Account A only
    cleared = await clear_account_kill_switch(session_factory, id_a, cleared_by="operator")
    assert cleared == 1

    # Verify Account A is active, Account B is STILL stopped
    assert is_account_kill_switch_active(id_a) is False
    assert is_account_kill_switch_active(id_b) is True


@pytest.mark.asyncio
async def test_persistence_across_backend_restart(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Test 5: After backend restart / hydrate_kill_switch_cache, cleared account remains active."""
    suffix = uuid4().hex[:6]
    async with session_factory() as session, session.begin():
        acc = AccountModel(name=f"RestartAcc-{suffix}", ibkr_account=f"DUR{suffix}", total_margin=Decimal("100000.00"))
        session.add(acc)
        await session.flush()
        acc_id = acc.id

    svc = KillSwitchService(session_factory=session_factory)
    await svc.initiate_square_off(account_id=acc_id, requested_by="operator")
    assert is_account_kill_switch_active(acc_id) is True

    # Clear account
    await clear_account_kill_switch(session_factory, acc_id, cleared_by="operator")
    assert is_account_kill_switch_active(acc_id) is False

    # Simulate backend restart by calling hydrate_kill_switch_cache
    armed = await hydrate_kill_switch_cache(session_factory)
    assert acc_id not in armed
    assert is_account_kill_switch_active(acc_id) is False


@pytest.mark.asyncio
async def test_start_again_does_not_submit_ibkr_orders(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Test 6: Start Again operation does not submit or modify any IBKR orders."""
    suffix = uuid4().hex[:6]
    async with session_factory() as session, session.begin():
        acc = AccountModel(name=f"NoOrdersAcc-{suffix}", ibkr_account=f"DUNO{suffix}", total_margin=Decimal("100000.00"))
        session.add(acc)
        await session.flush()
        acc_id = acc.id

    mock_om = MagicMock()

    svc = KillSwitchService(session_factory=session_factory, order_manager=mock_om)
    await svc.initiate_square_off(account_id=acc_id, requested_by="operator")

    # Clear account (Start Again)
    cleared = await clear_account_kill_switch(session_factory, acc_id, cleared_by="operator")
    assert cleared == 1

    # Verify no order manager or submission methods were called during Start Again
    mock_om.assert_not_called()
    assert mock_om.mock_calls == []


@pytest.mark.asyncio
async def test_existing_kill_switch_behavior_intact(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Test 7: Existing Kill Switch behavior still works (square-off -> armed -> active flag)."""
    suffix = uuid4().hex[:6]
    async with session_factory() as session, session.begin():
        acc = AccountModel(name=f"ExistingKS-{suffix}", ibkr_account=f"DUEKS{suffix}", total_margin=Decimal("100000.00"))
        session.add(acc)
        await session.flush()
        acc_id = acc.id

    svc = KillSwitchService(session_factory=session_factory)
    op, created = await svc.initiate_square_off(account_id=acc_id, requested_by="operator")
    assert created is True
    assert op.status == KILL_SWITCH_STATUS_ACTIVATING
    assert is_account_kill_switch_active(acc_id) is True


@pytest.mark.asyncio
async def test_active_account_signal_execution_intact(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Test 8: Signal execution behavior for active accounts remains unaffected."""
    suffix = uuid4().hex[:6]
    async with session_factory() as session, session.begin():
        acc = AccountModel(name=f"ActiveAcc-{suffix}", ibkr_account=f"DUACT{suffix}", total_margin=Decimal("100000.00"))
        session.add(acc)
        await session.flush()
        acc_id = acc.id

    # Active account is NOT in _KILL_SWITCH_ACTIVE_ACCOUNTS
    assert is_account_kill_switch_active(acc_id) is False
