"""Comprehensive unit tests for Production MFT concurrency, idempotency, and recovery."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.broker.ibkr.scheduler import IBKRExecutionScheduler
from app.db.models.signal import (
    JOB_STATUS_CLAIMED,
    JOB_STATUS_QUEUED,
)
from app.db.repositories.signal_repository import SignalJobRepository
from app.db.session import create_engine_from_settings
from app.services.worker_pool import compute_idempotency_key


@pytest.fixture
async def session_factory():
    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_compute_idempotency_key_deterministic():
    payload_a = {
        "strategy": "model_blue",
        "trade_id": "TRADE-100",
        "action": "OPEN",
    }
    payload_b = {
        "strategy": "model_blue",
        "trade_id": "TRADE-100",
        "action": "OPEN",
    }
    strat_a, sig_a, _, key_a = compute_idempotency_key(payload_a)
    strat_b, sig_b, _, key_b = compute_idempotency_key(payload_b)

    assert strat_a == strat_b == "model_blue"
    assert sig_a == sig_b == "TRADE-100"
    assert key_a == key_b


@pytest.mark.asyncio
async def test_signal_job_repository_idempotent_creation(session_factory: async_sessionmaker[AsyncSession]):
    test_id = uuid4().hex[:8]
    async with session_factory() as session, session.begin():
        repo = SignalJobRepository(session)
        payload = {"strategy": "model_blue", "trade_id": f"TEST-IDEM-{test_id}", "action": "OPEN"}
        strat_id, sig_id, trade_id, key = compute_idempotency_key(payload)

        job1, created1 = await repo.create_job_if_not_exists(
            signal_id=sig_id,
            strategy_id=strat_id,
            trade_id=trade_id,
            idempotency_key=key,
            raw_payload=payload,
            capture_data=None,
            correlation_id="req-1",
        )
        assert created1 is True
        assert job1.status == JOB_STATUS_QUEUED

        job2, created2 = await repo.create_job_if_not_exists(
            signal_id=sig_id,
            strategy_id=strat_id,
            trade_id=trade_id,
            idempotency_key=key,
            raw_payload=payload,
            capture_data=None,
            correlation_id="req-2",
        )
        assert created2 is False
        assert str(job1.job_id) == str(job2.job_id)


@pytest.mark.asyncio
async def test_signal_job_repository_claim_skip_locked(session_factory: async_sessionmaker[AsyncSession]):
    test_id = uuid4().hex[:8]
    async with session_factory() as session, session.begin():
        repo = SignalJobRepository(session)
        payload = {"strategy": "model_blue", "trade_id": f"TEST-CLAIM-{test_id}", "action": "OPEN"}
        strat_id, sig_id, trade_id, key = compute_idempotency_key(payload)

        job, _ = await repo.create_job_if_not_exists(
            signal_id=sig_id,
            strategy_id=strat_id,
            trade_id=trade_id,
            idempotency_key=key,
            raw_payload=payload,
            capture_data=None,
            correlation_id="req-claim",
        )
        target_job_id = job.job_id

    # Worker alpha claims
    async with session_factory() as session1, session1.begin():
        repo1 = SignalJobRepository(session1)
        claimed_jobs = await repo1.claim_next_jobs("worker-alpha", limit=10)
        claimed_ids = [j.job_id for j in claimed_jobs]
        assert target_job_id in claimed_ids
        claimed_target = next(j for j in claimed_jobs if j.job_id == target_job_id)
        assert claimed_target.worker_id == "worker-alpha"
        assert claimed_target.status == JOB_STATUS_CLAIMED

    # Worker beta attempts to claim while worker alpha lease is active
    async with session_factory() as session2, session2.begin():
        repo2 = SignalJobRepository(session2)
        claimed_again = await repo2.claim_next_jobs("worker-beta", limit=10)
        claimed_again_ids = [j.job_id for j in claimed_again]
        assert target_job_id not in claimed_again_ids


@pytest.mark.asyncio
async def test_ibkr_execution_scheduler_pacing():
    scheduler = IBKRExecutionScheduler(max_rate_per_sec=100.0, max_concurrent=5)
    counter = 0

    def mock_broker_call():
        nonlocal counter
        counter += 1
        return counter

    tasks = [scheduler.execute_paced(mock_broker_call) for _ in range(10)]
    results = await asyncio.gather(*tasks)

    assert len(results) == 10
    assert counter == 10
