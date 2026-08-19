"""Tests for account-scoped emergency Kill Switch / Square-Off-All endpoint."""

import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.accounts.config_service import AccountStrategyConfigService
from app.db.models.position import PositionModel
from app.db.session import create_engine_from_settings
from app.main import app


@pytest.fixture
def client():
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


@pytest.mark.asyncio
async def test_square_off_zero_positions(client: TestClient):
    """Square off endpoint with 0 open positions returns count 0."""
    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:6].upper()
    ibkr_acc = f"DU99{suffix}"

    async with factory() as session:
        svc = AccountStrategyConfigService(session)
        acc = await svc.create_account(
            name=f"Test Account {suffix}",
            ibkr_account=ibkr_acc,
            total_margin=Decimal("100000.00"),
            enabled=True,
        )
        await session.commit()
        acc_id = acc.id

    resp = client.post(f"/api/v1/config/accounts/{acc_id}/square-off")
    assert resp.status_code == 200
    body = resp.json()
    assert body["account_id"] == acc_id
    assert body["ibkr_account"] == ibkr_acc
    assert body["squared_off_count"] == 0
    assert body["trade_ids"] == []


@pytest.mark.asyncio
async def test_square_off_account_isolation(client: TestClient):
    """Square off endpoint targets ONLY positions for the specified account."""
    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    suffix_a = uuid.uuid4().hex[:6].upper()
    suffix_b = uuid.uuid4().hex[:6].upper()
    ibkr_a = f"DUA{suffix_a}"
    ibkr_b = f"DUB{suffix_b}"

    async with factory() as session:
        svc = AccountStrategyConfigService(session)
        acc_a = await svc.create_account(
            name=f"Account A {suffix_a}",
            ibkr_account=ibkr_a,
            total_margin=Decimal("100000.00"),
            enabled=True,
        )
        acc_b = await svc.create_account(
            name=f"Account B {suffix_b}",
            ibkr_account=ibkr_b,
            total_margin=Decimal("100000.00"),
            enabled=True,
        )
        await session.commit()
        acc_a_id = acc_a.id
        acc_b_id = acc_b.id

        trade_a = f"TR-A-{suffix_a}"
        trade_b = f"TR-B-{suffix_b}"

        pos_a = PositionModel(
            account_id=acc_a_id,
            trade_id=trade_a,
            strategy_id="model_blue",
            leg_a_symbol="SIL",
            leg_a_signed_qty=Decimal("100.00"),
            leg_a_entry_mark=Decimal("25.00"),
            leg_b_symbol="GDX",
            leg_b_signed_qty=Decimal("-100.00"),
            leg_b_entry_mark=Decimal("30.00"),
            target=Decimal("500.00"),
            stop=Decimal("250.00"),
            time_limit=3600,
            risk_state="OPEN",
        )
        pos_b = PositionModel(
            account_id=acc_b_id,
            trade_id=trade_b,
            strategy_id="model_blue",
            leg_a_symbol="SIL",
            leg_a_signed_qty=Decimal("50.00"),
            leg_a_entry_mark=Decimal("25.00"),
            target=Decimal("500.00"),
            stop=Decimal("250.00"),
            time_limit=3600,
            risk_state="OPEN",
        )
        session.add_all([pos_a, pos_b])
        await session.commit()

    # Square off Account A only via HTTP
    resp_a = client.post(f"/api/v1/config/accounts/{acc_a_id}/square-off")
    assert resp_a.status_code == 200
    body_a = resp_a.json()
    assert body_a["account_id"] == acc_a_id
    assert body_a["squared_off_count"] == 1
    assert body_a["trade_ids"] == [trade_a]

    # Verify Account B open position remains untouched in database
    async with factory() as session:
        b_pos = (
            await session.execute(
                select(PositionModel).where(
                    PositionModel.account_id == acc_b_id,
                    PositionModel.trade_id == trade_b,
                )
            )
        ).scalar_one_or_none()
        assert b_pos is not None
        assert b_pos.risk_state == "OPEN"
