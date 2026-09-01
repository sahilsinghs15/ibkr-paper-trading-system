"""Worker terminal status when post-submit failures occur."""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.account import AccountModel
from app.db.models.order import OrderModel
from app.db.models.signal import (
    JOB_STATUS_FAILED,
    JOB_STATUS_RECOVERY_REQUIRED,
    SignalJobModel,
    SignalModel,
)
from app.db.session import create_engine_from_settings
from app.oms.models import FanoutExecutionResult
from app.services.worker_pool import ExecutionWorkerPool


def _job(**overrides) -> SignalJobModel:
    job = MagicMock(spec=SignalJobModel)
    job.job_id = overrides.get("job_id", uuid4())
    job.signal_id = overrides.get("signal_id", f"T-WK-{uuid4().hex[:8]}")
    job.strategy_id = overrides.get("strategy_id", "model_blue")
    job.trade_id = overrides.get("trade_id", job.signal_id)
    job.attempt_count = overrides.get("attempt_count", 1)
    job.correlation_id = "corr-1"
    job.raw_payload = {
        "strategy": "model_blue",
        "trade_id": job.trade_id,
        "action": "OPEN",
    }
    job.capture_data = {}
    return job


@pytest.mark.asyncio
async def test_execute_job_quarantines_on_unexpected_fanout_error() -> None:
    om = MagicMock()
    om.parse_inbound_payload = MagicMock(return_value=MagicMock())
    om.process_signal_execution = AsyncMock(
        return_value=FanoutExecutionResult(had_unexpected_error=True)
    )
    factory = MagicMock()
    pool = ExecutionWorkerPool(factory, om, worker_count=1)
    statuses: list[str] = []

    async def capture_status(job_id, status, worker_id, lease_lost, error=None):
        statuses.append(status)
        return True

    with patch.object(pool, "_write_status", capture_status):
        await pool._execute_job("w1", _job(), asyncio.Event())

    assert JOB_STATUS_RECOVERY_REQUIRED in statuses


@pytest.mark.asyncio
async def test_execute_job_failed_when_exception_and_no_orders_emitted() -> None:
    om = MagicMock()
    om.parse_inbound_payload = MagicMock(return_value=MagicMock())
    om.process_signal_execution = AsyncMock(
        side_effect=RuntimeError("dictionary changed size during iteration")
    )
    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    pool = ExecutionWorkerPool(factory, om, worker_count=1)
    statuses: list[str] = []

    async def capture_status(job_id, status, worker_id, lease_lost, error=None):
        statuses.append(status)
        return True

    try:
        with patch.object(pool, "_write_status", capture_status):
            await pool._execute_job("w1", _job(), asyncio.Event())
        assert JOB_STATUS_FAILED in statuses
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_execute_job_quarantines_when_exception_and_orders_emitted() -> None:
    om = MagicMock()
    om.parse_inbound_payload = MagicMock(return_value=MagicMock())
    om.process_signal_execution = AsyncMock(
        side_effect=RuntimeError("dictionary changed size during iteration")
    )
    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    trade_id = f"T-EMIT-{uuid4().hex[:8]}"
    job = _job(signal_id=trade_id, trade_id=trade_id)
    pool = ExecutionWorkerPool(factory, om, worker_count=1)
    statuses: list[str] = []

    async with factory() as session, session.begin():
        account = AccountModel(
            name=f"wk-{uuid4().hex[:8]}",
            ibkr_account=f"DU{uuid4().hex[:8]}",
            total_margin=100000,
            enabled=True,
        )
        session.add(account)
        await session.flush()
        sig = SignalModel(
            signal_id=trade_id,
            strategy_id="model_blue",
            trade_id=trade_id,
            action="OPEN",
            pair="XLE",
            side="BUY",
            ref_price_a=Decimal(50),
            raw_payload={"test": True},
            status="NEW",
        )
        session.add(sig)
        await session.flush()
        session.add(
            OrderModel(
                signal_id=sig.id,
                trade_id=trade_id,
                internal_order_id=f"int-{trade_id}",
                account_id=account.id,
                strategy_id="model_blue",
                leg="L0",
                symbol="XLE",
                ibkr_contract="XLE-STK-SMART-USD",
                buy_sell="BUY",
                quantity=Decimal(10),
                limit_price=Decimal(0),
                status="FILLED",
            )
        )

    async def capture_status(job_id, status, worker_id, lease_lost, error=None):
        statuses.append(status)
        return True

    try:
        with patch.object(pool, "_write_status", capture_status):
            await pool._execute_job("w1", job, asyncio.Event())
        assert JOB_STATUS_RECOVERY_REQUIRED in statuses
    finally:
        await engine.dispose()
