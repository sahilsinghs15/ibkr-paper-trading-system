"""Comprehensive unit tests for Production MFT concurrency, idempotency, and recovery."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.signal import (
    JOB_STATUS_CLAIMED,
    JOB_STATUS_DEAD_LETTER,
    JOB_STATUS_PROCESSING,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RECOVERY_REQUIRED,
    SignalJobModel,
)
from app.db.repositories.execution_claim_repository import ExecutionClaimRepository
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
        await session.execute(text("DELETE FROM signal_jobs WHERE status IN ('QUEUED', 'RECEIVED', 'CLAIMED')"))
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
        claimed_jobs = await repo1.claim_next_jobs("worker-alpha", limit=1000)
        claimed_ids = [j.job_id for j in claimed_jobs]
        assert target_job_id in claimed_ids

    # Worker beta attempts to claim while worker alpha lease is active
    async with session_factory() as session2, session2.begin():
        repo2 = SignalJobRepository(session2)
        claimed_again = await repo2.claim_next_jobs("worker-beta", limit=1000)
        claimed_again_ids = [j.job_id for j in claimed_again]
        assert target_job_id not in claimed_again_ids


@pytest.mark.asyncio
async def test_concurrent_opens_cannot_both_pass_at_90_percent_headroom():
    from app.rms.checks.margin import MarginCheck
    from app.rms.models import (
        MarginPolicy,
        OrderAction,
        OrderIntent,
        OrderLeg,
        OrderSide,
        RMSContext,
        RMSOutcome,
    )
    from app.services.account_margin import AccountMarginSnapshot
    from app.services.order_manager import OrderManager

    account = "DU1"
    policy = MarginPolicy(
        check_enabled=True,
        min_free_buffer=Decimal(0),
        min_free_pct_of_netliq=Decimal(0),
        comfort_ratio=Decimal("0.80"),
        default_rate=Decimal("0.50"),
        confirm_borderline=False,
        rate_safety_multiplier=Decimal(1),
    )
    snap = AccountMarginSnapshot(
        ibkr_account=account,
        as_of=datetime.now(UTC),
        available_funds=Decimal(5500),
        net_liquidation=Decimal(10000),
        max_age_sec=300,
    )
    mgr = OrderManager(oms=None, rms_context=RMSContext(margin_policy=policy))
    mgr._rms_context.margin_snapshots[account] = snap

    def _intent(signal_id: str, symbol: str) -> OrderIntent:
        return OrderIntent(
            signal_id=signal_id,
            strategy_id="MODEL_BLUE",
            action=OrderAction.OPEN,
            ibkr_account=account,
            account_id=7,
            legs=[
                OrderLeg(
                    symbol=symbol,
                    side=OrderSide.BUY,
                    quantity=100,
                    price=Decimal(100),
                    contract_month="2026-09",
                    notional=Decimal(10000),
                )
            ],
        )

    outcomes: list[RMSOutcome] = []

    async def _run(intent: OrderIntent) -> None:
        async with mgr._exposure_guard(intent):
            result = MarginCheck().evaluate(intent, mgr._rms_context)
            if result.outcome == RMSOutcome.PASS:
                mgr._commit_margin(intent, opening=True)
            outcomes.append(result.outcome)

    await asyncio.gather(_run(_intent("A", "AAPL")), _run(_intent("B", "MSFT")))
    assert outcomes.count(RMSOutcome.PASS) == 1
    assert outcomes.count(RMSOutcome.REJECT) == 1


@pytest.mark.asyncio
async def test_concurrent_opens_cannot_both_pass_model_value_ceiling():
    from app.rms.checks.model_market_value import ModelMarketValueCheck
    from app.rms.market_value import intent_market_value
    from app.rms.models import (
        OrderAction,
        OrderIntent,
        OrderLeg,
        OrderSide,
        RMSContext,
        RMSOutcome,
        model_value_key,
    )
    from app.services.order_manager import OrderManager

    key = model_value_key(
        OrderIntent(
            signal_id="seed",
            strategy_id="MODEL_BLUE",
            action=OrderAction.OPEN,
            account_id=7,
            legs=[],
        )
    )
    mgr = OrderManager(
        oms=None,
        rms_context=RMSContext(
            market_value_check_enabled=True,
            model_value_limit={key: Decimal("10000")},
            model_value_used={key: Decimal("9000")},
        ),
    )

    def _intent(signal_id: str) -> OrderIntent:
        return OrderIntent(
            signal_id=signal_id,
            strategy_id="MODEL_BLUE",
            action=OrderAction.OPEN,
            account_id=7,
            legs=[
                OrderLeg(
                    symbol="XLE",
                    side=OrderSide.BUY,
                    quantity=1,
                    price=Decimal(1000),
                    contract_month="2026-09",
                    notional=Decimal(1000),
                )
            ],
        )

    outcomes: list[RMSOutcome] = []

    async def _run(intent: OrderIntent) -> None:
        async with mgr._exposure_guard(intent):
            result = ModelMarketValueCheck().evaluate(intent, mgr._rms_context)
            if result.outcome == RMSOutcome.PASS:
                value_key = model_value_key(intent)
                mgr._rms_context.model_value_used[value_key] = (
                    mgr._rms_context.model_value_used.get(value_key, Decimal(0))
                    + intent_market_value(intent)
                )
            outcomes.append(result.outcome)

    await asyncio.gather(_run(_intent("A")), _run(_intent("B")))
    assert outcomes.count(RMSOutcome.PASS) == 1
    assert outcomes.count(RMSOutcome.REJECT) == 1


async def _insert_job(
    session: AsyncSession,
    *,
    signal_id: str,
    strategy_id: str,
    trade_id: str,
    action: str,
    status: str = JOB_STATUS_QUEUED,
    attempt_count: int = 0,
    lease_expires_at: datetime | None = None,
) -> SignalJobModel:
    now = datetime.now(UTC)
    job = SignalJobModel(
        signal_id=signal_id,
        strategy_id=strategy_id,
        trade_id=trade_id,
        status=status,
        idempotency_key=f"{signal_id}:{action}:{uuid4().hex[:8]}",
        raw_payload={"strategy": strategy_id, "trade_id": trade_id, "action": action},
        capture_data=None,
        correlation_id=f"corr-{uuid4().hex[:8]}",
        attempt_count=attempt_count,
        received_at=now,
        queued_at=now,
        lease_expires_at=lease_expires_at,
    )
    session.add(job)
    await session.flush()
    return job


@pytest.mark.asyncio
async def test_uncommitted_open_claim_blocks_close_claim(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """M18: CLOSE must not claim while OPEN claim is still uncommitted."""
    suffix = uuid4().hex[:8]
    trade_id = f"M18-{suffix}"
    strategy_id = "model_blue"
    async with session_factory() as setup, setup.begin():
        await setup.execute(
            text(
                "DELETE FROM signal_jobs WHERE status IN "
                "('QUEUED', 'RECEIVED', 'CLAIMED', 'PROCESSING')"
            )
        )
        await _insert_job(
            setup,
            signal_id=trade_id,
            strategy_id=strategy_id,
            trade_id=trade_id,
            action="OPEN",
        )
        await asyncio.sleep(0.01)
        close_job = await _insert_job(
            setup,
            signal_id=f"{trade_id}:CLOSE",
            strategy_id=strategy_id,
            trade_id=trade_id,
            action="CLOSE",
        )
        close_id = close_job.job_id

    session_a = session_factory()
    await session_a.begin()
    try:
        claimed_open = await SignalJobRepository(session_a).claim_next_jobs(
            "worker-open", limit=1
        )
        assert len(claimed_open) == 1
        assert claimed_open[0].signal_id == trade_id

        async def _claim_close() -> list[SignalJobModel]:
            async with session_factory() as session_b, session_b.begin():
                return await SignalJobRepository(session_b).claim_next_jobs(
                    "worker-close", limit=1
                )

        task = asyncio.create_task(_claim_close())
        await asyncio.sleep(0.3)
        assert not task.done(), "CLOSE claim should wait on advisory lock"
        await session_a.commit()
        close_claimed = await asyncio.wait_for(task, timeout=5)
        assert all(j.job_id != close_id for j in close_claimed)
        assert all(j.signal_id != f"{trade_id}:CLOSE" for j in close_claimed)
    finally:
        if session_a.in_transaction():
            await session_a.rollback()
        await session_a.close()


@pytest.mark.asyncio
async def test_expired_processing_is_not_claimable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """M23: expired PROCESSING stays for the reclaimer, not a worker retake."""
    suffix = uuid4().hex[:8]
    trade_id = f"M23-PROC-{suffix}"
    expired = datetime.now(UTC) - timedelta(seconds=60)
    async with session_factory() as session, session.begin():
        job = await _insert_job(
            session,
            signal_id=trade_id,
            strategy_id="model_blue",
            trade_id=trade_id,
            action="OPEN",
            status=JOB_STATUS_PROCESSING,
            attempt_count=1,
            lease_expires_at=expired,
        )
        job_id = job.job_id
        claimed = await SignalJobRepository(session).claim_next_jobs("worker-x", limit=10)
        assert all(j.job_id != job_id for j in claimed)
        stats = await SignalJobRepository(session).reclaim_stale_jobs()
        assert stats["quarantined"] >= 1
        await session.refresh(job)
        assert job.status == JOB_STATUS_RECOVERY_REQUIRED


@pytest.mark.asyncio
async def test_dead_letter_with_live_claim_quarantines(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """M23: max-attempt expiry with a live CLAIMED barrier is RECOVERY_REQUIRED."""
    suffix = uuid4().hex[:8]
    trade_id = f"M23-DL-{suffix}"
    expired = datetime.now(UTC) - timedelta(seconds=60)
    async with session_factory() as session, session.begin():
        job = await _insert_job(
            session,
            signal_id=trade_id,
            strategy_id="model_blue",
            trade_id=trade_id,
            action="OPEN",
            status=JOB_STATUS_PROCESSING,
            attempt_count=3,
            lease_expires_at=expired,
        )
        await ExecutionClaimRepository(session).acquire(
            dedupe_key=f"1:model_blue:{trade_id}",
            account_id=1,
            strategy_id="model_blue",
            signal_id=trade_id,
            action="OPEN",
        )
        stats = await SignalJobRepository(session).reclaim_stale_jobs(max_attempts=3)
        assert stats["dead_lettered"] == 0
        assert stats["quarantined"] >= 1
        await session.refresh(job)
        assert job.status == JOB_STATUS_RECOVERY_REQUIRED
        assert job.status != JOB_STATUS_DEAD_LETTER

