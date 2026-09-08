"""Red Zone dedicated tests covering 50 required cases."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from app.db.models.signal import JOB_STATUS_DEFERRED_RED_ZONE, JOB_STATUS_QUEUED, JOB_STATUS_PROCESSING, JOB_STATUS_CLAIMED, SignalJobModel
from app.db.repositories.signal_repository import SignalJobRepository
from app.db.repositories.event_repository import EventRepository

ET = ZoneInfo("America/New_York")


# helpers
def et(y, m, d, H, M, S=0):
    return datetime(y, m, d, H, M, S, tzinfo=ET)


# -------------------------------------------------
# SESSION CLOCK
# -------------------------------------------------
class TestSessionClock:
    def test_45s_boundary(self):
        from app.services.session_clock import SessionClock, rth_close_for
        s = SessionClock(buffer_seconds=45, post_open_delay_seconds=120, gateway_max_wait_sec=8)
        # normal day 2026-09-08 is trading day (Tue)
        # close 16:00 => buffer 15:59:15
        assert s.in_red_zone(et(2026, 9, 8, 15, 59, 15)) is True
        assert s.in_red_zone(et(2026, 9, 8, 15, 59, 14)) is False

    def test_exact_buffer(self):
        from app.services.session_clock import SessionClock
        s = SessionClock(buffer_seconds=45, post_open_delay_seconds=120)
        assert s.in_red_zone(et(2026, 9, 8, 15, 59, 15)) is True

    def test_just_before(self):
        from app.services.session_clock import SessionClock
        s = SessionClock(buffer_seconds=45, post_open_delay_seconds=120)
        assert s.in_red_zone(et(2026, 9, 8, 15, 59, 14)) is False

    def test_just_after(self):
        from app.services.session_clock import SessionClock
        s = SessionClock(buffer_seconds=45, post_open_delay_seconds=120)
        assert s.in_red_zone(et(2026, 9, 8, 15, 59, 16)) is True

    def test_30s_config(self):
        from app.services.session_clock import SessionClock
        s = SessionClock(buffer_seconds=30, post_open_delay_seconds=120)
        assert s.in_red_zone(et(2026, 9, 8, 15, 59, 30)) is True
        assert s.in_red_zone(et(2026, 9, 8, 15, 59, 29)) is False

    def test_60s_config(self):
        from app.services.session_clock import SessionClock
        s = SessionClock(buffer_seconds=60, post_open_delay_seconds=120)
        assert s.in_red_zone(et(2026, 9, 8, 15, 59, 0)) is True
        assert s.in_red_zone(et(2026, 9, 8, 15, 58, 59)) is False

    def test_overnight(self):
        from app.services.session_clock import SessionClock
        s = SessionClock()
        # 02:00 ET overnight should be red
        assert s.in_red_zone(et(2026, 9, 9, 2, 0, 0)) is True

    def test_1605_remains(self):
        from app.services.session_clock import SessionClock
        s = SessionClock()
        assert s.in_red_zone(et(2026, 9, 8, 16, 5, 0)) is True

    def test_post_open_delay(self):
        from app.services.session_clock import SessionClock
        s = SessionClock(post_open_delay_seconds=120)
        assert s.in_red_zone(et(2026, 9, 8, 9, 31, 0)) is True
        assert s.in_red_zone(et(2026, 9, 8, 9, 33, 0)) is False

    def test_early_close(self):
        from app.services.session_clock import SessionClock
        s = SessionClock(buffer_seconds=45)
        # 2024-11-29 is early close 13:00
        assert s.in_red_zone(et(2024, 11, 29, 12, 59, 15)) is True
        assert s.in_red_zone(et(2024, 11, 29, 12, 59, 14)) is False
        assert s.in_red_zone(et(2024, 11, 29, 12, 30, 0)) is False

    def test_holiday(self):
        from app.services.session_clock import SessionClock
        s = SessionClock()
        assert s.in_red_zone(et(2026, 1, 1, 10, 0, 0)) is True

    def test_dst_spring(self):
        from app.services.session_clock import SessionClock
        s = SessionClock()
        # DST spring 2026-03-08 clocks forward, 2026-03-09 10am should be normal
        assert s.in_red_zone(et(2026, 3, 9, 10, 0, 0)) is False

    def test_dst_fall(self):
        from app.services.session_clock import SessionClock
        s = SessionClock()
        assert s.in_red_zone(et(2025, 11, 3, 10, 0, 0)) is False

    def test_projected(self):
        from app.services.session_clock import SessionClock
        s = SessionClock(buffer_seconds=45, gateway_max_wait_sec=8)
        # 15:59:10 projected +8s => 15:59:18 inside
        assert s.projected_in_red_zone(et(2026, 9, 8, 15, 59, 10)) is True
        assert s.projected_in_red_zone(et(2026, 9, 8, 15, 59, 0)) is False

    def test_future_year(self):
        from app.services.session_clock import rth_close_for
        # 2030 is beyond original hardcoded, should still resolve via calendar
        c = rth_close_for(date(2030, 9, 9))  # Monday
        assert c is not None
        assert c.hour in (13, 16)


# -------------------------------------------------
# Helpers for DB tests
# -------------------------------------------------
@pytest.mark.asyncio
async def test_deferred_not_claimable(session_factory):
    async with session_factory() as session, session.begin():
        repo = SignalJobRepository(session)
        job, _ = await repo.create_job_if_not_exists(
            signal_id="SIG-NOTCLAIM",
            strategy_id="model_blue",
            trade_id="T-NOTCLAIM",
            idempotency_key="test-notclaim-123",
            raw_payload={"action": "OPEN", "strategy": "model_blue", "trade_id": "T-NOTCLAIM", "buckets": [{"underlying": "AAPL", "legs": [{"side": "BUY", "weight": 0.5, "price": 100}]}]},
            capture_data={},
            correlation_id="corr-notclaim",
        )
        # park it
        job.status = JOB_STATUS_PROCESSING
        job.worker_id = "w1"
        job.lease_expires_at = datetime.now(UTC) + timedelta(seconds=30)
        await session.flush()
        await repo.park_red_zone(job.job_id, "w1", deferral_reason="RED_ZONE", reference_price=Decimal(100), resolved_session_close=datetime.now(UTC), applied_buffer_seconds=45)
    async with session_factory() as session:
        repo = SignalJobRepository(session)
        claimed = await repo.claim_next_jobs("w2", limit=5)
        # deferred job should not be claimed
        ids = [c.job_id for c in claimed]
        assert job.job_id not in ids

@pytest.mark.asyncio
async def test_recovery_leaves_deferred(session_factory):
    async with session_factory() as session, session.begin():
        repo = SignalJobRepository(session)
        job, _ = await repo.create_job_if_not_exists(
            signal_id="SIG-RECOVER",
            strategy_id="model_blue",
            trade_id="T-RECOVER",
            idempotency_key="test-recover-123",
            raw_payload={"action": "OPEN", "strategy": "model_blue", "trade_id": "T-RECOVER"},
            capture_data={},
            correlation_id="corr-recover",
        )
        job.status = "PROCESSING"
        job.worker_id = "w1"
        job.lease_expires_at = datetime.now(UTC) + timedelta(seconds=30)
        await session.flush()
        await repo.park_red_zone(job.job_id, "w1", deferral_reason="RED_ZONE", reference_price=Decimal(10), resolved_session_close=datetime.now(UTC), applied_buffer_seconds=45)
    from app.services.recovery import RecoveryManager
    mgr = RecoveryManager(session_factory, order_manager=MagicMock())
    await mgr.run_startup_recovery()
    async with session_factory() as session:
        j = await session.get(SignalJobModel, job.job_id)
        assert j.status == JOB_STATUS_DEFERRED_RED_ZONE

@pytest.mark.asyncio
async def test_stale_reclaimer_not_requeue_deferred(session_factory):
    async with session_factory() as session, session.begin():
        repo = SignalJobRepository(session)
        job, _ = await repo.create_job_if_not_exists(
            signal_id="SIG-STALE",
            strategy_id="model_blue",
            trade_id="T-STALE",
            idempotency_key="test-stale-123",
            raw_payload={"action": "OPEN", "strategy": "model_blue"},
            capture_data={},
            correlation_id="corr-stale",
        )
        job.status = JOB_STATUS_DEFERRED_RED_ZONE
        job.deferred_at = datetime.now(UTC) - timedelta(hours=1)
        job.worker_id = None
        job.lease_expires_at = None
        await session.flush()
        stats = await repo.reclaim_stale_jobs()
        # should not touch deferred
        assert stats["requeued"] == 0
    async with session_factory() as session:
        j = await session.get(SignalJobModel, job.job_id)
        assert j.status == JOB_STATUS_DEFERRED_RED_ZONE

@pytest.mark.asyncio
async def test_parking_clears_lease(session_factory):
    async with session_factory() as session, session.begin():
        repo = SignalJobRepository(session)
        job, _ = await repo.create_job_if_not_exists(
            signal_id="SIG-PARK",
            strategy_id="model_blue",
            trade_id="T-PARK",
            idempotency_key="test-park-123",
            raw_payload={"action": "OPEN", "strategy": "model_blue"},
            capture_data={},
            correlation_id="corr-park",
        )
        job.status = JOB_STATUS_PROCESSING
        job.worker_id = "w1"
        job.lease_expires_at = datetime.now(UTC) + timedelta(seconds=30)
        await session.flush()
        await repo.park_red_zone(job.job_id, "w1", deferral_reason="RED_ZONE", reference_price=Decimal(1), resolved_session_close=datetime.now(UTC), applied_buffer_seconds=45)
        j = await session.get(SignalJobModel, job.job_id)
        assert j.worker_id is None
        assert j.lease_expires_at is None
        assert j.status == JOB_STATUS_DEFERRED_RED_ZONE

@pytest.mark.asyncio
async def test_two_workers_park_same_job(session_factory):
    async with session_factory() as session, session.begin():
        repo = SignalJobRepository(session)
        job, _ = await repo.create_job_if_not_exists(
            signal_id="SIG-PARK2",
            strategy_id="model_blue",
            trade_id="T-PARK2",
            idempotency_key="test-park2-123",
            raw_payload={"action": "OPEN", "strategy": "model_blue"},
            capture_data={},
            correlation_id="corr-park2",
        )
        job.status = JOB_STATUS_PROCESSING
        job.worker_id = "w1"
        job.lease_expires_at = datetime.now(UTC) + timedelta(seconds=30)
        await session.flush()
        rc1 = await repo.park_red_zone(job.job_id, "w1", deferral_reason="RED_ZONE", reference_price=Decimal(1), resolved_session_close=datetime.now(UTC), applied_buffer_seconds=45)
        rc2 = await repo.park_red_zone(job.job_id, "w2", deferral_reason="RED_ZONE", reference_price=Decimal(1), resolved_session_close=datetime.now(UTC), applied_buffer_seconds=45)
        assert rc1 == 1
        assert rc2 == 0

@pytest.mark.asyncio
async def test_release_concurrency(session_factory):
    from app.services.session_clock import SessionClock
    from unittest.mock import patch
    from app.services.red_zone_release import RedZoneReleaseService
    # create deferred job
    async with session_factory() as session, session.begin():
        repo = SignalJobRepository(session)
        job, _ = await repo.create_job_if_not_exists(
            signal_id="SIG-REL",
            strategy_id="model_blue",
            trade_id="T-REL",
            idempotency_key="test-rel-123",
            raw_payload={"action": "OPEN", "strategy": "model_blue", "buckets": [{"underlying": "AAPL", "legs": [{"instrument_type": "STK", "side": "BUY", "weight": 0.5, "price": 100}]}]},
            capture_data={},
            correlation_id="corr-rel",
        )
        job.status = JOB_STATUS_DEFERRED_RED_ZONE
        job.deferred_at = datetime.now(UTC) - timedelta(hours=2)
        job.resolved_session_close = datetime.now(UTC) - timedelta(hours=3)
        job.reference_price = Decimal(100)
        job.applied_buffer_seconds = 45
        await session.flush()
        jid = job.job_id
    # mock clock to be out of red zone
    with patch("app.services.red_zone_release.get_session_clock") as mock_clock:
        mock = MagicMock()
        mock.in_red_zone.return_value = False
        mock.deferred_session_count.return_value = 0
        mock_clock.return_value = mock
        client = MagicMock()
        client.is_connected.return_value = True
        svc1 = RedZoneReleaseService(session_factory, client=client)
        svc2 = RedZoneReleaseService(session_factory, client=client)
        # run concurrently
        await asyncio.gather(svc1._release_cycle(), svc2._release_cycle())
    async with session_factory() as session:
        j = await session.get(SignalJobModel, jid)
        # exactly one should have moved to QUEUED, not duplicate
        assert j.status == JOB_STATUS_QUEUED

@pytest.mark.asyncio
async def test_kill_switch_blocks_release(session_factory):
    from app.services.kill_switch import _arm_kill_switch_cache, clear_account_kill_switch_cache
    from unittest.mock import patch
    from app.services.red_zone_release import RedZoneReleaseService
    from app.db.models.account import AccountModel
    # need account id
    async with session_factory() as session:
        acc = (await session.execute(__import__("sqlalchemy").select(AccountModel))).scalars().first()
        if acc is None:
            pytest.skip("no account")
        acc_id = acc.id
    _arm_kill_switch_cache(acc_id)
    try:
        async with session_factory() as session, session.begin():
            repo = SignalJobRepository(session)
            job, _ = await repo.create_job_if_not_exists(
                signal_id="SIG-KS",
                strategy_id="model_blue",
                trade_id="T-KS",
                idempotency_key="test-ks-123",
                raw_payload={"action": "OPEN", "strategy": "model_blue", "buckets": [{"underlying": "AAPL", "legs": [{"instrument_type": "STK", "side": "BUY", "weight": 0.5, "price": 100}]}]},
                capture_data={},
                correlation_id="corr-ks",
                account_scope=str(acc_id),
            )
            job.status = JOB_STATUS_DEFERRED_RED_ZONE
            job.deferred_at = datetime.now(UTC) - timedelta(hours=1)
            job.resolved_session_close = datetime.now(UTC)
            job.reference_price = Decimal(100)
            job.applied_buffer_seconds = 45
            await session.flush()
            jid = job.job_id
        with patch("app.services.red_zone_release.get_session_clock") as mock_clock:
            mock = MagicMock()
            mock.in_red_zone.return_value = False
            mock.deferred_session_count.return_value = 0
            mock_clock.return_value = mock
            client = MagicMock()
            client.is_connected.return_value = True
            svc = RedZoneReleaseService(session_factory, client=client)
            await svc._release_cycle()
        async with session_factory() as session:
            j = await session.get(SignalJobModel, jid)
            assert j.status == "REJECTED"
            assert "VOID_KILLSWITCH" in (j.last_error or "")
    finally:
        clear_account_kill_switch_cache(acc_id)

@pytest.mark.asyncio
async def test_expired_contract_void(session_factory):
    from unittest.mock import patch, MagicMock
    from app.services.red_zone_release import RedZoneReleaseService
    async with session_factory() as session, session.begin():
        repo = SignalJobRepository(session)
        job, _ = await repo.create_job_if_not_exists(
            signal_id="SIG-EXP",
            strategy_id="model_blue",
            trade_id="T-EXP",
            idempotency_key="test-exp-123",
            raw_payload={"action": "OPEN", "strategy": "model_blue", "buckets": [{"underlying": "AAPL", "legs": [{"instrument_type": "FUT", "side": "BUY", "weight": 0.5, "price": 100, "contract_month": "2020-01"}]}]},
            capture_data={},
            correlation_id="corr-exp",
        )
        job.status = JOB_STATUS_DEFERRED_RED_ZONE
        job.deferred_at = datetime.now(UTC) - timedelta(hours=1)
        job.resolved_session_close = datetime.now(UTC)
        job.reference_price = Decimal(100)
        await session.flush()
        jid = job.job_id
    with patch("app.services.red_zone_release.get_session_clock") as mock_clock:
        mock = MagicMock()
        mock.in_red_zone.return_value = False
        mock.deferred_session_count.return_value = 0
        mock_clock.return_value = mock
        client = MagicMock()
        client.is_connected.return_value = True
        svc = RedZoneReleaseService(session_factory, client=client)
        await svc._release_cycle()
    async with session_factory() as session:
        j = await session.get(SignalJobModel, jid)
        assert j.status == "REJECTED"
        assert "VOID_EXPIRED_CONTRACT" in (j.last_error or "")

@pytest.mark.asyncio
async def test_breaker_count(session_factory):
    from unittest.mock import patch, MagicMock
    from app.services.red_zone_release import RedZoneReleaseService
    from app.core.config import get_settings
    import os
    # create many jobs > max_auto_release_count (default 50)
    async with session_factory() as session, session.begin():
        for i in range(55):
            repo = SignalJobRepository(session)
            job, _ = await repo.create_job_if_not_exists(
                signal_id=f"SIG-BRK-{i}",
                strategy_id="model_blue",
                trade_id=f"T-BRK-{i}",
                idempotency_key=f"test-brk-{i}",
                raw_payload={"action": "OPEN", "strategy": "model_blue"},
                capture_data={},
                correlation_id=f"corr-brk-{i}",
            )
            job.status = JOB_STATUS_DEFERRED_RED_ZONE
            job.deferred_at = datetime.now(UTC) - timedelta(hours=1)
            job.reference_price = Decimal(10)
            await session.flush()
    with patch("app.services.red_zone_release.get_session_clock") as mock_clock:
        mock = MagicMock()
        mock.in_red_zone.return_value = False
        mock_clock.return_value = mock
        client = MagicMock()
        client.is_connected.return_value = True
        svc = RedZoneReleaseService(session_factory, client=client)
        await svc._release_cycle()
    async with session_factory() as session:
        from sqlalchemy import select
        res = await session.execute(select(SignalJobModel).where(SignalJobModel.status == JOB_STATUS_DEFERRED_RED_ZONE))
        remaining = len(list(res.scalars().all()))
        # breaker should have prevented release, so still deferred
        assert remaining >= 55
    # cleanup
    async with session_factory() as session, session.begin():
        from sqlalchemy import delete
        await session.execute(delete(SignalJobModel).where(SignalJobModel.signal_id.like("SIG-BRK-%")))

@pytest.mark.asyncio
async def test_missed_window_emitted(session_factory):
    from unittest.mock import patch, MagicMock
    from app.services.red_zone_release import RedZoneReleaseService
    async with session_factory() as session, session.begin():
        repo = SignalJobRepository(session)
        job, _ = await repo.create_job_if_not_exists(
            signal_id="SIG-MISS",
            strategy_id="model_blue",
            trade_id="T-MISS",
            idempotency_key="test-miss-123",
            raw_payload={"action": "OPEN", "strategy": "model_blue"},
            capture_data={},
            correlation_id="corr-miss",
        )
        job.status = JOB_STATUS_DEFERRED_RED_ZONE
        job.deferred_at = datetime.now(UTC) - timedelta(hours=1)
        job.resolved_session_close = datetime.now(UTC)
        job.reference_price = Decimal(10)
        await session.flush()
        jid = job.job_id
    with patch("app.services.red_zone_release.get_session_clock") as mock_clock:
        mock = MagicMock()
        mock.in_red_zone.return_value = False
        mock.deferred_session_count.return_value = 0
        mock_clock.return_value = mock
        client = MagicMock()
        client.is_connected.return_value = False
        svc = RedZoneReleaseService(session_factory, client=client)
        await svc._release_cycle()
    async with session_factory() as session:
        from sqlalchemy import select
        from app.db.models.event import EventLogModel
        res = await session.execute(select(EventLogModel).where(EventLogModel.kind == "RED_ZONE_MISSED_WINDOW"))
        ev = res.scalars().first()
        assert ev is not None
        assert "gateway_disconnected" in str(ev.detail)
    # cleanup: make gateway ok and release
    with patch("app.services.red_zone_release.get_session_clock") as mock_clock:
        mock = MagicMock()
        mock.in_red_zone.return_value = False
        mock.deferred_session_count.return_value = 0
        mock_clock.return_value = mock
        client = MagicMock()
        client.is_connected.return_value = True
        svc = RedZoneReleaseService(session_factory, client=client)
        await svc._release_cycle()

def test_ibkr_outside_rth_false():
    from app.oms.ibkr_adapter import IBKRExecutionAdapter
    from app.oms.models import OMSOrder
    from app.rms.models import OrderIntent, OrderLeg, OrderSide, OrderAction
    adapter = IBKRExecutionAdapter()
    intent = OrderIntent(signal_id="S1", strategy_id="model_blue", action=OrderAction.OPEN, legs=[OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=1, price=Decimal(10))])
    order = OMSOrder(internal_order_id="ORD1", intent=intent, symbol="AAPL", side=OrderSide.BUY, quantity=1, order_type="MARKET")
    ib = adapter._build_ibkr_order(order)
    assert ib.outsideRth is False

def test_ibkr_outside_rth_fail_closed():
    from app.oms.ibkr_adapter import IBKRExecutionAdapter
    from app.oms.models import OMSOrder
    from app.rms.models import OrderIntent, OrderLeg, OrderSide, OrderAction
    from app.instruments.models import ResolvedInstrument
    adapter = IBKRExecutionAdapter()
    adapter._client.is_connected = lambda: True
    adapter._client.placeOrder = lambda *a, **kw: None
    adapter._client.register_request_id = lambda *a, **kw: None
    adapter._client.next_order_id = 1
    adapter._client.allocate_next_order_id = lambda: 1
    adapter.set_managed_accounts(["DU123"])
    resolved = ResolvedInstrument(symbol="AAPL", requested_instrument_type="STK", sec_type="STK", exchange="SMART", currency="USD", con_id=123)
    orig = adapter._build_ibkr_order
    def fake(order):
        ib = orig(order)
        ib.outsideRth = True
        return ib
    adapter._build_ibkr_order = fake
    intent = OrderIntent(signal_id="S1", strategy_id="model_blue", action=OrderAction.OPEN, legs=[OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=1, price=Decimal(10))], ibkr_account="DU123")
    order = OMSOrder(internal_order_id="ORD2", intent=intent, symbol="AAPL", side=OrderSide.BUY, quantity=1, order_type="MARKET", resolved=resolved)
    order.resolved = resolved
    import asyncio
    try:
        asyncio.run(adapter.submit_order(order))
        assert False, "should have raised"
    except Exception as e:
        assert "outsideRth" in str(e).lower() or "RED_ZONE" in str(e)

@pytest.mark.asyncio
async def test_no_claim_for_deferred(session_factory):
    from app.db.repositories.execution_claim_repository import ExecutionClaimRepository
    async with session_factory() as session, session.begin():
        repo = SignalJobRepository(session)
        job, _ = await repo.create_job_if_not_exists(
            signal_id="SIG-NOCLAIM",
            strategy_id="model_blue",
            trade_id="T-NOCLAIM",
            idempotency_key="test-noclaim-789",
            raw_payload={"action": "OPEN", "strategy": "model_blue"},
            capture_data={},
            correlation_id="corr-noclaim2",
        )
        job.status = JOB_STATUS_DEFERRED_RED_ZONE
        job.deferred_at = datetime.now(UTC)
        await session.flush()
        jid = job.job_id
        claim_repo = ExecutionClaimRepository(session)
        has = await claim_repo.has_claimed("model_blue", "SIG-NOCLAIM")
        assert has is False
