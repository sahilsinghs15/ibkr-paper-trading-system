"""Tests for POST /api/v1/reconcile/positions/flatten."""

from collections.abc import Generator
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.account import AccountModel
from app.db.repositories.broker_position_repository import BrokerPositionRepository
from app.db.repositories.position_repository import PositionRepository
from app.main import app
from app.oms.basket import BasketExecutionResult, BasketState
from app.oms.models import OMSOrderStatus
from app.rms.models import OrderAction, OrderSide
from app.schemas.reconcile_schemas import FlattenBrokerPositionResponse
from app.services.broker_flatten_service import BrokerFlattenService


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
            "app.services.order_manager.OrderManager.hydrate_live_pnl",
            new_callable=AsyncMock,
        ),
        TestClient(app) as c,
    ):
        yield c



def _filled_order(side: OrderSide, qty: float, symbol: str = "AAPL") -> MagicMock:
    order = MagicMock()
    order.status = OMSOrderStatus.FILLED
    order.is_compensation = False
    order.filled_quantity = qty
    order.side = side
    order.symbol = symbol
    return order


@pytest.mark.asyncio
async def test_broker_flatten_submits_market_reverse_and_skips_ledger(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    test_id = uuid4().hex[:8]
    ibkr_account = f"DU-FLAT-{test_id}"
    con_id = 900000 + int(test_id[:4], 16) % 10000

    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"FlatAcc-{test_id}",
            ibkr_account=ibkr_account,
            total_margin=Decimal("100000"),
        )
        session.add(acc)
        await session.flush()
        account_id = acc.id

        repo = BrokerPositionRepository(session)
        await repo.replace_snapshot(
            [
                {
                    "ibkr_account": ibkr_account,
                    "con_id": con_id,
                    "account_id": account_id,
                    "symbol": "AAPL",
                    "sec_type": "CFD",
                    "currency": "USD",
                    "exchange": "SMART",
                    "signed_qty": Decimal("25"),
                    "avg_cost": Decimal("150"),
                }
            ],
            as_of=datetime.now(UTC),
        )

    mock_baskets = MagicMock()
    captured: dict[str, object] = {}

    async def fake_execute(intent, rms_pass, order_type="LIMIT"):
        captured["intent"] = intent
        captured["order_type"] = order_type
        captured["reason"] = rms_pass.reason
        b_obj = MagicMock()
        b_obj.state = BasketState.CLOSED
        return BasketExecutionResult(
            basket=b_obj,
            intent=intent,
            orders=[_filled_order(OrderSide.SELL, 25.0)],
        )

    mock_baskets.execute = AsyncMock(side_effect=fake_execute)

    mock_order_manager = MagicMock()
    mock_order_manager._baskets = mock_baskets
    mock_order_manager._resolve_instruments = AsyncMock(side_effect=lambda intent: intent)

    svc = BrokerFlattenService(
        session_factory=session_factory,
        order_manager=mock_order_manager,
    )

    with patch.object(PositionRepository, "close_trade", AsyncMock()) as mock_close_trade:
        result = await svc.flatten_line(
            ibkr_account=ibkr_account,
            symbol="AAPL",
            sec_type="CFD",
            con_id=con_id,
        )

    assert result.success is True
    assert result.status == "FLAT"
    assert result.side == "SELL"
    assert result.quantity == 25.0

    intent = captured["intent"]
    assert captured["order_type"] == "MARKET"
    assert captured["reason"] == "RECONCILE_BROKER_FLATTEN"
    assert intent.action == OrderAction.CLOSE
    assert intent.ibkr_account == ibkr_account
    assert len(intent.legs) == 1
    assert intent.legs[0].symbol == "AAPL"
    assert intent.legs[0].side == OrderSide.SELL
    assert float(intent.legs[0].quantity) == 25.0
    assert intent.legs[0].con_id == con_id
    mock_close_trade.assert_not_called()


def test_broker_flatten_http_endpoint_returns_schema(client: TestClient) -> None:
    fake_response = FlattenBrokerPositionResponse(
        ibkr_account="DU1",
        account_id=1,
        symbol="AAPL",
        sec_type="CFD",
        con_id=111,
        side="SELL",
        quantity=10.0,
        status="FLAT",
        success=True,
        message="ok",
    )

    with patch(
        "app.api.routes.reconcile.BrokerFlattenService.flatten_line",
        new_callable=AsyncMock,
        return_value=fake_response,
    ):
        response = client.post(
            "/api/v1/reconcile/positions/flatten",
            json={
                "ibkr_account": "DU1",
                "symbol": "AAPL",
                "sec_type": "CFD",
                "con_id": 111,
            },
        )

    assert response.status_code == 200, response.text
    payload = FlattenBrokerPositionResponse.model_validate(response.json())
    assert payload.status == "FLAT"
    assert payload.success is True
