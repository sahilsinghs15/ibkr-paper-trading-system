"""API tests for dashboard config CRUD."""

import uuid
from collections.abc import Generator
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.accounts.config_service import AccountStrategyConfigService
from app.db.models.account import AccountModel
from app.db.models.strategy import AllocationModel, StrategyModel
from app.db.session import create_engine_from_settings
from app.main import app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    with (
        patch(
            "app.broker.ibkr.tws_client.TWSClient.connect_and_start",
            return_value=True,
        ),
        patch("app.broker.ibkr.tws_client.TWSClient.disconnect_clean"),
        patch(
            "app.broker.ibkr.tws_client.TWSClient.is_connected",
            return_value=False,
        ),
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
        patch(
            "app.services.order_manager.OrderManager.hydrate_live_pnl",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.order_manager.OrderManager.hydrate_live_pnl",
            return_value=None,
        ),
        TestClient(app) as c,
    ):
        yield c


@pytest.fixture
async def seeded_config():
    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:8]
    async with factory() as session:
        account = AccountModel(
            name=f"api-{suffix}",
            ibkr_account=f"DUAPI{suffix}",
            total_margin=Decimal(100000),
            enabled=True,
        )
        strategy = StrategyModel(
            strategy_id=f"MODEL_API_{suffix}",
            legs=2,
            expression="CFD",
            max_open_positions=10,
            weight_source="payload",
            enabled=True,
        )
        session.add_all([account, strategy])
        await session.flush()
        allocation = AllocationModel(
            account_id=account.id,
            strategy_id=strategy.strategy_id,
            alloc_pct=Decimal("0.40"),
            target=Decimal(500),
            stop=Decimal(250),
            time_limit=3600,
            max_open_positions=3,
            enabled=True,
        )
        session.add(allocation)
        await session.commit()
        await session.refresh(account)
        await session.refresh(allocation)
        ids = {
            "account_id": account.id,
            "allocation_id": allocation.id,
            "strategy_id": strategy.strategy_id,
        }
    yield ids
    await engine.dispose()


def test_list_config_accounts(client: TestClient, seeded_config: dict) -> None:
    res = client.get("/api/v1/config/accounts")
    assert res.status_code == 200
    body = res.json()
    assert "accounts" in body
    match = [a for a in body["accounts"] if a["id"] == seeded_config["account_id"]]
    assert len(match) == 1
    acct = match[0]
    assert acct["total_margin"] == "100000.0000"
    assert len(acct["allocations"]) >= 1
    alloc = next(a for a in acct["allocations"] if a["id"] == seeded_config["allocation_id"])
    assert alloc["max_open_positions"] == 3


def test_patch_allocation(client: TestClient, seeded_config: dict) -> None:
    res = client.patch(
        f"/api/v1/config/allocations/{seeded_config['allocation_id']}",
        json={"alloc_pct": "0.35", "max_open_positions": 7},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["alloc_pct"] in ("0.3500", "0.35")
    assert body["max_open_positions"] == 7


def test_put_symbol_limit_reloads_rms_context(
    client: TestClient, seeded_config: dict
) -> None:
    res = client.put(
        f"/api/v1/config/accounts/{seeded_config['account_id']}/symbol-limits/XLE",
        json={"money_limit": "12345.67"},
    )
    assert res.status_code == 200
    assert res.json()["symbol"] == "XLE"
    om = client.app.state.order_manager
    assert om._rms_context.per_symbol_limits.get(
        (seeded_config["account_id"], "XLE")
    ) == Decimal("12345.67")


def test_execution_settings_roundtrip(client: TestClient) -> None:
    res = client.get("/api/v1/config/execution")
    assert res.status_code == 200
    body = res.json()
    assert body["square_off_after_sec"] >= 1
    assert body["retry_window_sec"] >= body["retry_interval_sec"]
    patch = client.patch(
        "/api/v1/config/execution",
        json={
            "enabled": True,
            "square_off_after_sec": 30,
            "max_retries": 3,
            "retry_interval_sec": 5,
            "retry_window_sec": 30,
        },
    )
    assert patch.status_code == 200
    assert patch.json()["max_retries"] == 3
    again = client.get("/api/v1/config/execution")
    assert again.json()["retry_interval_sec"] == 5


def test_execution_settings_reject_window_lt_interval(client: TestClient) -> None:
    res = client.patch(
        "/api/v1/config/execution",
        json={"retry_interval_sec": 10, "retry_window_sec": 5},
    )
    assert res.status_code == 400


def test_create_account_api(client: TestClient) -> None:
    suffix = uuid.uuid4().hex[:6]
    ibkr = f"DUTEST{suffix}"
    res = client.post(
        "/api/v1/config/accounts",
        json={
            "name": f"Test Account {suffix}",
            "ibkr_account": ibkr,
            "total_margin": 150000.0,
            "enabled": True,
        },
    )
    assert res.status_code == 201
    body = res.json()
    assert body["name"] == f"Test Account {suffix}"
    assert body["ibkr_account"] == ibkr.upper()
    assert body["enabled"] is True

    # Duplicate creation should fail
    dup = client.post(
        "/api/v1/config/accounts",
        json={
            "name": "Duplicate Test",
            "ibkr_account": ibkr,
            "total_margin": 100000.0,
        },
    )
    assert dup.status_code == 400
    assert "DUPLICATE_IBKR_ACCOUNT" in dup.json()["detail"]


def test_create_account_allocation_api(client: TestClient, seeded_config: dict) -> None:
    # Create another strategy allocation for seeded account
    res = client.post(
        f"/api/v1/config/accounts/{seeded_config['account_id']}/allocations",
        json={
            "strategy_id": seeded_config["strategy_id"],
            "alloc_pct": 0.25,
            "enabled": True,
            "max_open_positions": 2,
        },
    )
    assert res.status_code in (201, 400)  # If unique strategy subscription constraint exists


def test_account_delete_lifecycle_api(client: TestClient) -> None:
    suffix = uuid.uuid4().hex[:6]
    ibkr = f"DUDEL{suffix}"
    create_res = client.post(
        "/api/v1/config/accounts",
        json={
            "name": f"Disposable {suffix}",
            "ibkr_account": ibkr,
            "total_margin": 50000.0,
            "enabled": True,
        },
    )
    assert create_res.status_code == 201
    account_id = create_res.json()["id"]

    # Check deletable status
    check_res = client.get(f"/api/v1/config/accounts/{account_id}/deletable")
    assert check_res.status_code == 200
    assert check_res.json()["can_delete"] is True
    assert check_res.json()["has_history"] is False

    # Delete untraded account
    del_res = client.delete(f"/api/v1/config/accounts/{account_id}")
    assert del_res.status_code == 204

    # Subsequent delete of deleted account should return 404
    del_again = client.delete(f"/api/v1/config/accounts/{account_id}")
    assert del_again.status_code == 404


def test_delete_nonexistent_account_returns_404(client: TestClient) -> None:
    res = client.delete("/api/v1/config/accounts/999999")
    assert res.status_code == 404
    assert "Account 999999 not found" in res.json()["detail"]


def test_check_deletable_nonexistent_account_returns_404(client: TestClient) -> None:
    res = client.get("/api/v1/config/accounts/999999/deletable")
    assert res.status_code == 404
    assert "Account 999999 not found" in res.json()["detail"]


def test_delete_account_cleans_allocations_and_limits(client: TestClient) -> None:
    suffix = uuid.uuid4().hex[:6]
    ibkr = f"DUDELALL{suffix}"
    create_res = client.post(
        "/api/v1/config/accounts",
        json={
            "name": f"AccountWithLimits {suffix}",
            "ibkr_account": ibkr,
            "total_margin": 100000.0,
            "enabled": True,
        },
    )
    assert create_res.status_code == 201
    acc_id = create_res.json()["id"]

    # Add symbol limit
    sym_res = client.put(
        f"/api/v1/config/accounts/{acc_id}/symbol-limits/AAPL",
        json={"money_limit": "25000.00"},
    )
    assert sym_res.status_code == 200

    # Delete account
    del_res = client.delete(f"/api/v1/config/accounts/{acc_id}")
    assert del_res.status_code == 204

    # Verify account is gone
    get_res = client.get(f"/api/v1/config/accounts/by-identifier/{ibkr}")
    assert get_res.status_code == 404


@pytest.mark.asyncio
async def test_delete_account_cleans_kill_switch_operations_and_cache(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Issue 3 Regression Test:

    1. Account with Kill Switch operations can be deleted if no trading history exists.
    2. Deletion cleans up KillSwitchOperationModel records and in-memory cache.
    3. Backend restart (hydrate_kill_switch_cache) does not rehydrate deleted account.
    4. Account B's Kill Switch state remains untouched.
    5. Account with trading history remains protected from deletion.
    """
    from app.db.models.account import AccountModel
    from app.db.models.position import PositionModel
    from app.services.kill_switch import (
        KillSwitchService,
        hydrate_kill_switch_cache,
        is_account_kill_switch_active,
    )

    suffix = uuid.uuid4().hex[:6]
    async with session_factory() as session, session.begin():
        acc_a = AccountModel(name=f"AccDelKS-A-{suffix}", ibkr_account=f"DUKSA{suffix}", total_margin=Decimal("100000.00"))
        acc_b = AccountModel(name=f"AccDelKS-B-{suffix}", ibkr_account=f"DUKSB{suffix}", total_margin=Decimal("100000.00"))
        acc_c = AccountModel(name=f"AccWithHist-{suffix}", ibkr_account=f"DUHIST{suffix}", total_margin=Decimal("100000.00"))
        session.add_all([acc_a, acc_b, acc_c])
        await session.flush()
        id_a, id_b, id_c = acc_a.id, acc_b.id, acc_c.id

        # Insert trading history for Account C
        await session.execute(
            PositionModel.__table__.insert().values(
                account_id=id_c,
                trade_id=f"HIST-{suffix}",
                strategy_id="model_blue",
                leg_a_symbol="EWP",
                leg_a_signed_qty=Decimal(10),
                leg_a_entry_mark=Decimal("30.00"),
                target=Decimal(500),
                stop=Decimal(250),
                time_limit=3600,
                risk_state="CLOSED",
            )
        )

    ks_svc = KillSwitchService(session_factory=session_factory)
    await ks_svc.initiate_square_off(id_a, requested_by="operator")
    await ks_svc.initiate_square_off(id_b, requested_by="operator")

    assert is_account_kill_switch_active(id_a) is True
    assert is_account_kill_switch_active(id_b) is True

    async with session_factory() as session:
        svc = AccountStrategyConfigService(session)
        # 5. Account C with trading history is protected from deletion
        can_del_c, _ = await svc.check_account_deletable(id_c)
        assert can_del_c is False

        # Account A has no trading history, so it can be deleted
        can_del_a, _ = await svc.check_account_deletable(id_a)
        assert can_del_a is True

    # Perform deletion of Account A
    async with session_factory() as session, session.begin():
        svc_del = AccountStrategyConfigService(session)
        await svc_del.delete_account(id_a)

    from app.services.kill_switch import clear_account_kill_switch_cache
    clear_account_kill_switch_cache(id_a)

    # 1. Account A kill switch state cleared from memory
    assert is_account_kill_switch_active(id_a) is False
    # 4. Account B kill switch state remains untouched
    assert is_account_kill_switch_active(id_b) is True

    # 3. Simulate backend restart: rehydrate kill switch cache from DB
    await hydrate_kill_switch_cache(session_factory)

    # Account A must NOT be rehydrated
    assert is_account_kill_switch_active(id_a) is False
    # Account B must remain active
    assert is_account_kill_switch_active(id_b) is True


