"""API integration tests for Trading Pause endpoints (Requirement 9)."""

from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.db.models.account import AccountModel
from app.main import app
from app.services.trading_pause import is_account_trading_paused


@pytest.fixture
async def session_factory():
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    sf = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    yield sf
    await engine.dispose()


@pytest.fixture
def client():
    with (
        patch("app.broker.ibkr.tws_client.TWSClient.connect_and_start", return_value=True),
        patch("app.broker.ibkr.tws_client.TWSClient.disconnect_clean"),
        patch("app.broker.ibkr.tws_client.TWSClient.is_connected", return_value=True),
        patch("app.services.worker_pool.ExecutionWorkerPool.start", new_callable=AsyncMock),
        patch("app.services.worker_pool.ExecutionWorkerPool.stop", new_callable=AsyncMock),
        patch(
            "app.services.position_reconciler.PositionReconciler.start",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.position_reconciler.PositionReconciler.stop",
            new_callable=AsyncMock,
        ),
        patch("app.services.recovery.RecoveryManager.run_startup_recovery", new_callable=AsyncMock),
        patch("app.services.order_manager.OrderManager.hydrate_live_pnl", new_callable=AsyncMock),
        patch("app.services.order_manager.OrderManager.hydrate_runtime_from_db", new_callable=AsyncMock),
        TestClient(app) as c,
    ):
        yield c


@pytest.mark.asyncio
async def test_trading_pause_api_round_trip(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
):
    """Verifies GET, POST, and clear endpoints for trading pause, idempotency, and account serialization."""
    suffix = uuid4().hex[:6]
    ibkr_acc = f"DUAPI{suffix}"

    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"ApiPauseTest-{suffix}",
            ibkr_account=ibkr_acc,
            total_margin=Decimal("100000.00"),
        )
        session.add(acc)
        await session.flush()
        acc_id = acc.id

    # 1. GET initial status: trading_paused=False
    get_res1 = client.get(f"/api/v1/config/accounts/{acc_id}/trading-pause")
    assert get_res1.status_code == 200, get_res1.text
    data1 = get_res1.json()
    assert data1["account_id"] == acc_id
    assert data1["ibkr_account"] == ibkr_acc
    assert data1["trading_paused"] is False
    assert data1["paused_at"] is None
    assert data1["paused_by"] is None

    # 2. POST pause: returns trading_paused=True
    post_res1 = client.post(f"/api/v1/config/accounts/{acc_id}/trading-pause")
    assert post_res1.status_code == 200, post_res1.text
    data2 = post_res1.json()
    assert data2["account_id"] == acc_id
    assert data2["trading_paused"] is True
    assert data2["paused_at"] is not None
    assert data2["paused_by"] is not None
    assert is_account_trading_paused(acc_id) is True

    # 3. GET /config/accounts confirms trading_paused exposed on list and detail
    list_res = client.get("/api/v1/config/accounts")
    assert list_res.status_code == 200
    accounts = list_res.json()["accounts"]
    matching = next((a for a in accounts if a["id"] == acc_id), None)
    assert matching is not None
    assert matching["trading_paused"] is True
    assert matching["paused_at"] is not None

    # 4. POST pause again (idempotency): 200 OK, still paused
    post_res2 = client.post(f"/api/v1/config/accounts/{acc_id}/trading-pause")
    assert post_res2.status_code == 200
    assert post_res2.json()["trading_paused"] is True

    # 5. POST clear: trading_paused becomes False
    clear_res1 = client.post(f"/api/v1/config/accounts/{acc_id}/trading-pause/clear")
    assert clear_res1.status_code == 200, clear_res1.text
    data3 = clear_res1.json()
    assert data3["trading_paused"] is False
    assert data3["paused_at"] is None
    assert data3["paused_by"] is None
    assert is_account_trading_paused(acc_id) is False

    # 6. POST clear again (idempotency): 200 OK, still False
    clear_res2 = client.post(f"/api/v1/config/accounts/{acc_id}/trading-pause/clear")
    assert clear_res2.status_code == 200
    assert clear_res2.json()["trading_paused"] is False

    # 7. Nonexistent account returns 404
    bad_res = client.get("/api/v1/config/accounts/99999999/trading-pause")
    assert bad_res.status_code == 404
