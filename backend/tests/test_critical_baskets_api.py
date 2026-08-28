"""Tests for GET /api/v1/baskets/critical."""

from collections.abc import Generator
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.account import AccountModel
from app.db.models.basket import BasketModel
from app.db.models.order import OrderModel
from app.db.models.signal import SignalModel
from app.main import app


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
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
            "app.services.critical_recovery.CriticalRecoveryService.enqueue_all_critical",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.order_manager.OrderManager.hydrate_live_pnl",
            new_callable=AsyncMock,
        ),
        TestClient(app) as c,
    ):
        yield c


@pytest.mark.asyncio
async def test_list_critical_baskets_api(
    session_factory: async_sessionmaker[AsyncSession],
    client: TestClient,
) -> None:
    test_id = uuid4().hex[:8]
    ibkr_account = f"DU-API-{test_id}"

    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"Api-{test_id}",
            ibkr_account=ibkr_account,
            total_margin=Decimal("100000"),
            enabled=True,
        )
        session.add(acc)
        await session.flush()
        account_id = acc.id
        basket = BasketModel(
            account_id=account_id,
            trade_id=f"T-API-{test_id}",
            strategy_id="synthetic_n_leg",
            action="OPEN",
            state="CRITICAL",
            intended_leg_count=2,
            recovery_status="RECOVERING",
            recovery_detail="attempt 1",
        )
        session.add(basket)
        await session.flush()
        sig = SignalModel(
            signal_id=f"T-API-{test_id}",
            strategy_id="synthetic_n_leg",
            trade_id=f"T-API-{test_id}",
            action="OPEN",
            pair="XLE",
            side="BUY",
            ref_price_a=Decimal("100"),
            raw_payload={"test": True},
            status="FAILED",
        )
        session.add(sig)
        await session.flush()
        session.add(
            OrderModel(
                signal_id=sig.id,
                trade_id=f"T-API-{test_id}",
                internal_order_id=f"int-api-{test_id}",
                basket_id=basket.id,
                is_compensation=False,
                account_id=account_id,
                strategy_id="synthetic_n_leg",
                leg="L0",
                symbol="XLE",
                ibkr_contract="XLE-STK-SMART-USD:99901",
                buy_sell="BUY",
                quantity=Decimal("100"),
                limit_price=Decimal("0"),
                status="FILLED",
                fill_qty=Decimal("100"),
            )
        )

    resp = client.get(
        "/api/v1/baskets/critical",
        params={"ibkr_account": ibkr_account},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ibkr_account"] == ibkr_account
    assert len(body["incidents"]) == 1
    inc = body["incidents"][0]
    assert inc["trade_id"] == f"T-API-{test_id}"
    assert inc["recovery_status"] == "RECOVERING"
    assert len(inc["legs"]) == 1
    assert inc["legs"][0]["filled_qty"] == 100.0
    assert inc["legs"][0]["con_id"] == 99901


def test_list_critical_baskets_unknown_account(client: TestClient) -> None:
    resp = client.get(
        "/api/v1/baskets/critical",
        params={"ibkr_account": "DOESNOTEXIST999"},
    )
    assert resp.status_code == 404
