"""OPEN-pair exit threshold updates: repo, validation, audit event, HTTP."""

import uuid
from collections.abc import Generator
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.account import AccountModel
from app.db.models.event import EventLogModel
from app.db.repositories.position_repository import (
    PositionNotFoundError,
    PositionNotOpenError,
    PositionRepository,
)
from app.main import app
from app.models.model_blue_trade import OpenModelBlueTrade, OpenModelBlueTradeLeg
from app.rms.models import OrderSide


def _trade(trade_id: str) -> OpenModelBlueTrade:
    return OpenModelBlueTrade(
        trade_id=trade_id,
        strategy_id="model_blue",
        direction=1,
        legs=(
            OpenModelBlueTradeLeg(
                symbol="XLE",
                instrument_type="STK",
                side=OrderSide.BUY,
                quantity=Decimal(10),
                price=Decimal(80),
            ),
            OpenModelBlueTradeLeg(
                symbol="XOP",
                instrument_type="STK",
                side=OrderSide.SELL,
                quantity=Decimal(10),
                price=Decimal(80),
            ),
        ),
    )


async def _seed_open_pair(session: AsyncSession, *, suffix: str) -> tuple[int, str]:
    account = AccountModel(
        name=f"exit-{suffix}",
        ibkr_account=f"DUEXIT{suffix}",
        total_margin=Decimal(100000),
        enabled=True,
    )
    session.add(account)
    await session.flush()
    trade_id = f"MBG-EXIT-{suffix}"
    await PositionRepository(session).open_trade(
        _trade(trade_id),
        account_id=account.id,
        target=Decimal(500),
        stop=Decimal(250),
        time_limit=3600,
        target_unit="ABSOLUTE",
        stop_unit="ABSOLUTE",
        exit_automation_enabled=False,
    )
    return account.id, trade_id


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
        patch(
            "app.services.order_manager.OrderManager.reload_margin_rates",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.margin_scanner.MarginScanner.start_background",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.margin_scanner.MarginScanner.stop",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.critical_recovery.CriticalRecoveryService.enqueue_all_critical",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.risk_exit_monitor.RiskExitMonitor.start",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.risk_exit_monitor.RiskExitMonitor.stop",
            new_callable=AsyncMock,
        ),
        TestClient(app) as c,
    ):
        yield c


@pytest.fixture
async def seeded_open_pair(session_factory) -> dict[str, object]:
    suffix = uuid.uuid4().hex[:8]
    async with session_factory() as session:
        account_id, trade_id = await _seed_open_pair(session, suffix=suffix)
        await session.commit()
    return {"account_id": account_id, "trade_id": trade_id}


@pytest.fixture
async def seeded_closed_pair(session_factory) -> dict[str, object]:
    suffix = uuid.uuid4().hex[:8]
    async with session_factory() as session:
        account_id, trade_id = await _seed_open_pair(session, suffix=suffix)
        await PositionRepository(session).close_trade(
            trade_id,
            account_id=account_id,
            exit_marks={"XLE": Decimal(81), "XOP": Decimal(79)},
        )
        await session.commit()
    return {"account_id": account_id, "trade_id": trade_id}


@pytest.mark.asyncio
async def test_update_exit_thresholds_on_open_row(session_factory) -> None:
    suffix = uuid.uuid4().hex[:8]
    async with session_factory() as session:
        account_id, trade_id = await _seed_open_pair(session, suffix=suffix)
        await session.commit()

    async with session_factory() as session:
        repo = PositionRepository(session)
        row = await repo.update_exit_thresholds(
            account_id=account_id,
            trade_id=trade_id,
            target=Decimal(800),
            stop=Decimal("0.05"),
            target_unit="ABSOLUTE",
            stop_unit="PERCENT",
            exit_automation_enabled=True,
        )
        await session.commit()
        assert row.target == Decimal(800)
        assert row.stop == Decimal("0.05")
        assert row.stop_unit == "PERCENT"
        assert row.exit_automation_enabled is True


@pytest.mark.asyncio
async def test_update_exit_thresholds_refuses_closed(session_factory) -> None:
    suffix = uuid.uuid4().hex[:8]
    async with session_factory() as session:
        account_id, trade_id = await _seed_open_pair(session, suffix=suffix)
        await PositionRepository(session).close_trade(
            trade_id,
            account_id=account_id,
            exit_marks={"XLE": Decimal(81), "XOP": Decimal(79)},
        )
        await session.commit()

    async with session_factory() as session:
        repo = PositionRepository(session)
        with pytest.raises(PositionNotOpenError, match="POSITION_NOT_OPEN"):
            await repo.update_exit_thresholds(
                account_id=account_id,
                trade_id=trade_id,
                target=Decimal(1),
            )


@pytest.mark.asyncio
async def test_update_exit_thresholds_missing_row(session_factory) -> None:
    async with session_factory() as session:
        repo = PositionRepository(session)
        with pytest.raises(PositionNotFoundError):
            await repo.update_exit_thresholds(
                account_id=1,
                trade_id="NO-SUCH-TRADE",
                target=Decimal(1),
            )


def test_patch_exits_invalid_unit(client: TestClient, seeded_open_pair: dict) -> None:
    res = client.patch(
        f"/api/v1/config/accounts/{seeded_open_pair['account_id']}"
        f"/positions/{seeded_open_pair['trade_id']}/exits",
        json={"target_unit": "SHARES"},
    )
    assert res.status_code == 400
    assert "INVALID_EXIT_UNIT" in res.json()["detail"]


def test_patch_exits_invalid_percent(client: TestClient, seeded_open_pair: dict) -> None:
    res = client.patch(
        f"/api/v1/config/accounts/{seeded_open_pair['account_id']}"
        f"/positions/{seeded_open_pair['trade_id']}/exits",
        json={"stop": "1.5", "stop_unit": "PERCENT"},
    )
    assert res.status_code == 400
    assert "INVALID_EXIT_THRESHOLD" in res.json()["detail"]


def test_patch_exits_writes_event(
    client: TestClient, seeded_open_pair: dict
) -> None:
    account_id = seeded_open_pair["account_id"]
    trade_id = seeded_open_pair["trade_id"]
    res = client.patch(
        f"/api/v1/config/accounts/{account_id}/positions/{trade_id}/exits",
        json={
            "target": "900",
            "stop": "300",
            "target_unit": "ABSOLUTE",
            "stop_unit": "ABSOLUTE",
            "exit_automation_enabled": True,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert Decimal(body["target"]) == Decimal(900)
    assert body["exit_automation_enabled"] is True


@pytest.mark.asyncio
async def test_patch_exits_event_row_persisted(
    session_factory, client, seeded_open_pair
) -> None:
    account_id = seeded_open_pair["account_id"]
    trade_id = seeded_open_pair["trade_id"]
    res = client.patch(
        f"/api/v1/config/accounts/{account_id}/positions/{trade_id}/exits",
        json={"target": "750", "exit_automation_enabled": True},
    )
    assert res.status_code == 200, res.text
    async with session_factory() as session:
        ev = (
            await session.execute(
                select(EventLogModel)
                .where(
                    EventLogModel.kind == "PAIR_EXIT_THRESHOLDS_UPDATED",
                    EventLogModel.process == "config",
                )
                .order_by(EventLogModel.id.desc())
            )
        ).scalars().first()
        assert ev is not None
        assert ev.detail["trade_id"] == trade_id
        assert ev.detail["new"]["exit_automation_enabled"] is True


def test_patch_exits_closed_is_conflict(
    client: TestClient, seeded_closed_pair: dict
) -> None:
    res = client.patch(
        f"/api/v1/config/accounts/{seeded_closed_pair['account_id']}"
        f"/positions/{seeded_closed_pair['trade_id']}/exits",
        json={"target": "1"},
    )
    assert res.status_code == 409
    assert "POSITION_NOT_OPEN" in res.json()["detail"]
