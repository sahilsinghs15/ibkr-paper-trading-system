"""Tests for POST /api/v1/reconcile/positions/align."""

from collections.abc import Generator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.account import AccountModel
from app.db.models.basket import BasketModel
from app.db.models.instrument import InstrumentModel
from app.db.models.position import PositionModel
from app.db.repositories.broker_position_repository import BrokerPositionRepository
from app.db.repositories.position_repository import RISK_STATE_OPEN
from app.main import app
from app.oms.basket import BasketExecutionResult, BasketState
from app.oms.models import OMSOrderStatus
from app.rms.models import OrderAction, OrderSide
from app.schemas.reconcile_schemas import FlattenBrokerPositionResponse
from app.services.broker_align_service import BrokerAlignService


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
        patch(
            "app.services.worker_pool.ExecutionWorkerPool.start", new_callable=AsyncMock
        ),
        patch(
            "app.services.worker_pool.ExecutionWorkerPool.stop", new_callable=AsyncMock
        ),
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


def _unique_symbol(prefix: str) -> tuple[str, int]:
    token = uuid4().hex
    symbol = f"{prefix}{token.upper()}"
    con_id = 900000 + int(token[:6], 16) % 50000
    return symbol, con_id


def _filled_order(side: OrderSide, qty: float, symbol: str) -> MagicMock:
    order = MagicMock()
    order.status = OMSOrderStatus.FILLED
    order.is_compensation = False
    order.filled_quantity = qty
    order.side = side
    order.symbol = symbol
    return order


def _mock_order_manager(captured: dict[str, Any]) -> MagicMock:
    mock_baskets = MagicMock()

    async def fake_execute(intent, rms_pass, order_type="LIMIT"):
        captured["intent"] = intent
        captured["order_type"] = order_type
        captured["reason"] = rms_pass.reason
        b_obj = MagicMock()
        b_obj.state = BasketState.CLOSED
        side = intent.legs[0].side
        qty = float(intent.legs[0].quantity)
        symbol = intent.legs[0].symbol
        return BasketExecutionResult(
            basket=b_obj,
            intent=intent,
            orders=[_filled_order(side, qty, symbol)],
        )

    mock_baskets.execute = AsyncMock(side_effect=fake_execute)
    mock_order_manager = MagicMock()
    mock_order_manager._baskets = mock_baskets
    mock_order_manager._resolve_instruments = AsyncMock(
        side_effect=lambda intent: intent
    )
    return mock_order_manager


@pytest.mark.asyncio
async def test_broker_align_qty_drift_sells_excess(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    test_id = uuid4().hex[:8]
    ibkr_account = f"DU-DRIFT-{test_id}"
    symbol, con_id = _unique_symbol("DRF")

    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"DriftAcc-{test_id}",
            ibkr_account=ibkr_account,
            total_margin=Decimal(100000),
        )
        session.add(acc)
        await session.flush()
        account_id = acc.id

        session.add(
            InstrumentModel(
                symbol=symbol,
                sec_type="CFD",
                trade_conid=con_id,
                market_data_conid=con_id,
                underlying_exchange="NASDAQ",
                exchange="SMART",
                currency="USD",
                multiplier=Decimal(1),
            )
        )
        session.add(
            PositionModel(
                account_id=account_id,
                trade_id=f"T-{test_id}",
                strategy_id="model_blue",
                leg_a_symbol=symbol,
                leg_a_signed_qty=Decimal(60),
                leg_a_entry_mark=Decimal(150),
                leg_b_symbol=None,
                leg_b_signed_qty=None,
                leg_b_entry_mark=None,
                target=Decimal("0.05"),
                stop=Decimal("0.02"),
                time_limit=60,
                leg_a_instrument_type="CFD",
                leg_b_instrument_type=None,
                risk_state=RISK_STATE_OPEN,
            )
        )

        repo = BrokerPositionRepository(session)
        await repo.replace_snapshot(
            [
                {
                    "ibkr_account": ibkr_account,
                    "con_id": con_id,
                    "account_id": account_id,
                    "symbol": symbol,
                    "sec_type": "CFD",
                    "currency": "USD",
                    "exchange": "SMART",
                    "signed_qty": Decimal(100),
                    "avg_cost": Decimal(150),
                }
            ],
            as_of=datetime.now(UTC),
        )

    captured: dict[str, Any] = {}
    svc = BrokerAlignService(
        session_factory=session_factory,
        order_manager=_mock_order_manager(captured),
    )

    result = await svc.align_line(
        ibkr_account=ibkr_account,
        symbol=symbol,
        sec_type="CFD",
        con_id=con_id,
    )

    assert result.success is True
    assert result.status == "ALIGNED"
    assert result.side == "SELL"
    assert result.quantity == 40.0

    intent = captured["intent"]
    assert captured["order_type"] == "MARKET"
    assert captured["reason"] == "RECONCILE_BROKER_ALIGN"
    assert intent.action == OrderAction.CLOSE
    assert intent.legs[0].side == OrderSide.SELL
    assert float(intent.legs[0].quantity) == 40.0


@pytest.mark.asyncio
async def test_broker_align_orphan_flattens_to_zero(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    test_id = uuid4().hex[:8]
    ibkr_account = f"DU-ORPH-{test_id}"
    symbol, con_id = _unique_symbol("ORP")

    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"OrphAcc-{test_id}",
            ibkr_account=ibkr_account,
            total_margin=Decimal(100000),
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
                    "symbol": symbol,
                    "sec_type": "CFD",
                    "currency": "USD",
                    "exchange": "SMART",
                    "signed_qty": Decimal(50),
                    "avg_cost": Decimal(150),
                }
            ],
            as_of=datetime.now(UTC),
        )

    captured: dict[str, Any] = {}
    svc = BrokerAlignService(
        session_factory=session_factory,
        order_manager=_mock_order_manager(captured),
    )

    result = await svc.align_line(
        ibkr_account=ibkr_account,
        symbol=symbol,
        sec_type="CFD",
        con_id=con_id,
    )

    assert result.success is True
    assert result.side == "SELL"
    assert result.quantity == 50.0
    intent = captured["intent"]
    assert intent.action == OrderAction.CLOSE


@pytest.mark.asyncio
async def test_broker_align_ghost_buys_open(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    test_id = uuid4().hex[:8]
    ibkr_account = f"DU-GHOST-{test_id}"
    symbol, con_id = _unique_symbol("GHO")

    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"GhostAcc-{test_id}",
            ibkr_account=ibkr_account,
            total_margin=Decimal(100000),
        )
        session.add(acc)
        await session.flush()
        account_id = acc.id

        session.add(
            InstrumentModel(
                symbol=symbol,
                sec_type="CFD",
                trade_conid=con_id,
                market_data_conid=con_id,
                underlying_exchange="NASDAQ",
                exchange="SMART",
                currency="USD",
                multiplier=Decimal(1),
            )
        )
        session.add(
            PositionModel(
                account_id=account_id,
                trade_id=f"T-{test_id}",
                strategy_id="model_blue",
                leg_a_symbol=symbol,
                leg_a_signed_qty=Decimal(30),
                leg_a_entry_mark=Decimal(150),
                leg_b_symbol=None,
                leg_b_signed_qty=None,
                leg_b_entry_mark=None,
                target=Decimal("0.05"),
                stop=Decimal("0.02"),
                time_limit=60,
                leg_a_instrument_type="CFD",
                leg_b_instrument_type=None,
                risk_state=RISK_STATE_OPEN,
            )
        )

    captured: dict[str, Any] = {}
    svc = BrokerAlignService(
        session_factory=session_factory,
        order_manager=_mock_order_manager(captured),
    )

    result = await svc.align_line(
        ibkr_account=ibkr_account,
        symbol=symbol,
        sec_type="CFD",
        con_id=con_id,
    )

    assert result.success is True
    assert result.side == "BUY"
    assert result.quantity == 30.0
    intent = captured["intent"]
    assert intent.action == OrderAction.OPEN
    assert intent.legs[0].con_id == con_id


@pytest.mark.asyncio
async def test_broker_align_rejects_already_aligned(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    test_id = uuid4().hex[:8]
    ibkr_account = f"DU-MATCH-{test_id}"
    symbol, con_id = _unique_symbol("MAT")

    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"MatchAcc-{test_id}",
            ibkr_account=ibkr_account,
            total_margin=Decimal(100000),
        )
        session.add(acc)
        await session.flush()
        account_id = acc.id

        session.add(
            InstrumentModel(
                symbol=symbol,
                sec_type="CFD",
                trade_conid=con_id,
                market_data_conid=con_id,
                underlying_exchange="NASDAQ",
                exchange="SMART",
                currency="USD",
                multiplier=Decimal(1),
            )
        )
        session.add(
            PositionModel(
                account_id=account_id,
                trade_id=f"T-{test_id}",
                strategy_id="model_blue",
                leg_a_symbol=symbol,
                leg_a_signed_qty=Decimal(25),
                leg_a_entry_mark=Decimal(150),
                leg_b_symbol=None,
                leg_b_signed_qty=None,
                leg_b_entry_mark=None,
                target=Decimal("0.05"),
                stop=Decimal("0.02"),
                time_limit=60,
                leg_a_instrument_type="CFD",
                leg_b_instrument_type=None,
                risk_state=RISK_STATE_OPEN,
            )
        )

        repo = BrokerPositionRepository(session)
        await repo.replace_snapshot(
            [
                {
                    "ibkr_account": ibkr_account,
                    "con_id": con_id,
                    "account_id": account_id,
                    "symbol": symbol,
                    "sec_type": "CFD",
                    "currency": "USD",
                    "exchange": "SMART",
                    "signed_qty": Decimal(25),
                    "avg_cost": Decimal(150),
                }
            ],
            as_of=datetime.now(UTC),
        )

    svc = BrokerAlignService(
        session_factory=session_factory,
        order_manager=MagicMock(
            _baskets=MagicMock(),
            _resolve_instruments=AsyncMock(side_effect=lambda i: i),
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await svc.align_line(
            ibkr_account=ibkr_account,
            symbol=symbol,
            sec_type="CFD",
            con_id=con_id,
        )

    assert "already matches ledger" in exc_info.value.detail  # type: ignore[operator]


@pytest.mark.asyncio
async def test_broker_align_rejects_in_flight(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    test_id = uuid4().hex[:8]
    ibkr_account = f"DU-FLY-{test_id}"
    symbol, con_id = _unique_symbol("FLY")

    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"FlyAcc-{test_id}",
            ibkr_account=ibkr_account,
            total_margin=Decimal(100000),
        )
        session.add(acc)
        await session.flush()
        account_id = acc.id

        session.add(
            BasketModel(
                account_id=account_id,
                trade_id=f"B-{test_id}",
                strategy_id="model_blue",
                action="OPEN",
                state="EXECUTING",
                intended_leg_count=1,
            )
        )

        repo = BrokerPositionRepository(session)
        await repo.replace_snapshot(
            [
                {
                    "ibkr_account": ibkr_account,
                    "con_id": con_id,
                    "account_id": account_id,
                    "symbol": symbol,
                    "sec_type": "CFD",
                    "currency": "USD",
                    "exchange": "SMART",
                    "signed_qty": Decimal(50),
                    "avg_cost": Decimal(150),
                }
            ],
            as_of=datetime.now(UTC),
        )

    svc = BrokerAlignService(
        session_factory=session_factory,
        order_manager=MagicMock(
            _baskets=MagicMock(),
            _resolve_instruments=AsyncMock(side_effect=lambda i: i),
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await svc.align_line(
            ibkr_account=ibkr_account,
            symbol=symbol,
            sec_type="CFD",
            con_id=con_id,
        )

    assert "in-flight" in exc_info.value.detail  # type: ignore[operator]


@pytest.mark.asyncio
async def test_broker_align_ghost_without_catalog_uses_con_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    test_id = uuid4().hex[:8]
    ibkr_account = f"DU-NOINST-{test_id}"
    symbol, con_id = _unique_symbol("NIN")

    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"NoInstAcc-{test_id}",
            ibkr_account=ibkr_account,
            total_margin=Decimal(100000),
        )
        session.add(acc)
        await session.flush()
        account_id = acc.id

        session.add(
            PositionModel(
                account_id=account_id,
                trade_id=f"T-{test_id}",
                strategy_id="model_blue",
                leg_a_symbol=symbol,
                leg_a_signed_qty=Decimal(30),
                leg_a_entry_mark=Decimal(150),
                leg_b_symbol=None,
                leg_b_signed_qty=None,
                leg_b_entry_mark=None,
                target=Decimal("0.05"),
                stop=Decimal("0.02"),
                time_limit=60,
                leg_a_instrument_type="CFD",
                leg_b_instrument_type=None,
                risk_state=RISK_STATE_OPEN,
            )
        )

    captured: dict[str, Any] = {}
    svc = BrokerAlignService(
        session_factory=session_factory,
        order_manager=_mock_order_manager(captured),
    )

    result = await svc.align_line(
        ibkr_account=ibkr_account,
        symbol=symbol,
        sec_type="CFD",
        con_id=con_id,
    )

    assert result.success is True
    assert result.side == "BUY"
    assert result.quantity == 30.0
    intent = captured["intent"]
    assert intent.legs[0].con_id == con_id


def test_broker_align_http_endpoint_returns_schema(client: TestClient) -> None:
    fake_response = FlattenBrokerPositionResponse(
        ibkr_account="DU1",
        account_id=1,
        symbol="AAPL",
        sec_type="CFD",
        con_id=111,
        side="SELL",
        quantity=40.0,
        status="ALIGNED",
        success=True,
        message="ok",
    )

    with patch(
        "app.api.routes.reconcile.BrokerAlignService.align_line",
        new_callable=AsyncMock,
        return_value=fake_response,
    ):
        response = client.post(
            "/api/v1/reconcile/positions/align",
            json={
                "ibkr_account": "DU1",
                "symbol": "AAPL",
                "sec_type": "CFD",
                "con_id": 111,
            },
        )

    assert response.status_code == 200, response.text
    payload = FlattenBrokerPositionResponse.model_validate(response.json())
    assert payload.status == "ALIGNED"
    assert payload.success is True
