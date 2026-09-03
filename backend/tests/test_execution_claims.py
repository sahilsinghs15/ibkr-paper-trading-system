"""execution_claims: CLAIMED is emitted; never silent-release on zero ledger rows."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.account import AccountModel
from app.db.models.execution_claim import CLAIM_STATE_CLAIMED, CLAIM_STATE_EXECUTED
from app.db.models.order import OrderModel
from app.db.models.signal import SignalModel
from app.db.repositories.execution_claim_repository import ExecutionClaimRepository
from app.services.recovery import RecoveryManager


async def _account(session: AsyncSession) -> AccountModel:
    acc = AccountModel(
        name=f"claim-{uuid4().hex[:8]}",
        ibkr_account=f"DU{uuid4().hex[:6].upper()}",
        total_margin=1_000_000,
        enabled=True,
    )
    session.add(acc)
    await session.flush()
    return acc


@pytest.mark.asyncio
async def test_reconcile_stale_claimed_with_zero_orders_is_not_released(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Crash between persist and placeOrder: CLAIMED stays CLAIMED (M1)."""
    async with session_factory() as session, session.begin():
        acc = await _account(session)
        repo = ExecutionClaimRepository(session)
        claim = await repo.acquire(
            dedupe_key=f"{acc.id}:model_blue:CRASH-PERSIST-{uuid4().hex[:8]}",
            account_id=acc.id,
            strategy_id="model_blue",
            signal_id=f"CRASH-PERSIST-{uuid4().hex[:8]}",
            action="OPEN",
        )
        claim.claimed_at = datetime.now(UTC) - timedelta(seconds=600)
        await session.flush()
        dedupe = claim.dedupe_key
        strategy_id = claim.strategy_id
        signal_id = claim.signal_id

    async with session_factory() as session, session.begin():
        repo = ExecutionClaimRepository(session)
        stats = await repo.reconcile_stale_claims(stale_after_sec=1.0)
        assert stats["released"] == 0
        row = await repo.get(dedupe)
        assert row is not None
        assert row.state == CLAIM_STATE_CLAIMED
        assert await repo.has_claimed(strategy_id, signal_id) is True


@pytest.mark.asyncio
async def test_reconcile_stale_claimed_with_orders_seals_executed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Crash after persist (orders row exists): seal EXECUTED, do not requeue."""
    signal_token = f"CRASH-PLACE-{uuid4().hex[:8]}"
    async with session_factory() as session, session.begin():
        acc = await _account(session)
        sig = SignalModel(
            strategy_id="model_blue",
            signal_id=signal_token,
            pair="EWA:EWC",
            action="OPEN",
            side="BUY",
            ref_price_a=Decimal("25.00"),
            raw_payload={"source": "test"},
            status="NEW",
        )
        session.add(sig)
        await session.flush()
        session.add(
            OrderModel(
                internal_order_id=f"ORD-{uuid4().hex[:12]}",
                signal_id=sig.id,
                account_id=acc.id,
                trade_id=signal_token,
                strategy_id="model_blue",
                leg="L0",
                symbol="EWA",
                ibkr_contract="EWA-STK-SMART-USD",
                buy_sell="BUY",
                quantity=Decimal("10.00"),
                limit_price=Decimal("0.00"),
                status="SUBMITTED",
            )
        )
        repo = ExecutionClaimRepository(session)
        claim = await repo.acquire(
            dedupe_key=f"{acc.id}:model_blue:{signal_token}",
            account_id=acc.id,
            strategy_id="model_blue",
            signal_id=signal_token,
            action="OPEN",
        )
        claim.claimed_at = datetime.now(UTC) - timedelta(seconds=600)
        await session.flush()
        dedupe = claim.dedupe_key

    async with session_factory() as session, session.begin():
        repo = ExecutionClaimRepository(session)
        stats = await repo.reconcile_stale_claims(stale_after_sec=1.0)
        assert stats["sealed"] >= 1
        assert stats["released"] == 0
        row = await repo.get(dedupe)
        assert row is not None
        assert row.state == CLAIM_STATE_EXECUTED


@pytest.mark.asyncio
async def test_recovery_does_not_requeue_claimed_without_orders(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Restart with CLAIMED + nothing at IB: quarantine, not silent retake (M1/M38)."""
    from app.db.models.signal import (
        JOB_STATUS_PROCESSING,
        JOB_STATUS_RECOVERY_REQUIRED,
        SignalJobModel,
    )

    signal_token = f"CLAIMED-LIVE-{uuid4().hex[:8]}"
    async with session_factory() as session, session.begin():
        acc = await _account(session)
        job = SignalJobModel(
            signal_id=signal_token,
            strategy_id="model_blue",
            trade_id=signal_token,
            status=JOB_STATUS_PROCESSING,
            idempotency_key=f"idem-{uuid4().hex}",
            raw_payload={"source": "test"},
            correlation_id=f"corr-{uuid4().hex[:8]}",
            attempt_count=1,
            max_attempts=5,
        )
        session.add(job)
        repo = ExecutionClaimRepository(session)
        await repo.acquire(
            dedupe_key=f"{acc.id}:model_blue:{signal_token}",
            account_id=acc.id,
            strategy_id="model_blue",
            signal_id=signal_token,
            action="OPEN",
        )

    class _OM:
        async def hydrate_runtime_from_db(self) -> None:
            return None

        _oms = None

    mgr = RecoveryManager(session_factory, _OM())
    await mgr.run_startup_recovery()

    async with session_factory() as session:
        from sqlalchemy import select

        row = (
            await session.execute(
                select(SignalJobModel).where(SignalJobModel.signal_id == signal_token)
            )
        ).scalar_one()
        assert row.status == JOB_STATUS_RECOVERY_REQUIRED
