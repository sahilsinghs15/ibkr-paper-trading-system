"""API tests for dashboard config CRUD."""

import uuid
from collections.abc import Generator
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.account import AccountModel, PerSymbolLimitModel
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
            return_value=True,
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
            total_margin=Decimal("100000"),
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
            target=Decimal("500"),
            stop=Decimal("250"),
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
