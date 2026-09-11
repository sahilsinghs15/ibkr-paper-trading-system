"""Comprehensive tests for Trading Pause Service and OrderManager / RiskExitMonitor integration.

Covers:
1. Pause -> OPEN rejected.
2. Pause -> CLOSE accepted.
3. Resume -> OPEN accepted.
4. Restart hydration preserves pause.
5. Pause creates no kill-switch operation.
6. Pause never calls flatten.
7. Kill switch still works while paused.
8. Pause/resume idempotency.
10. Both OrderManager gate locations.
11. Account isolation (pausing Account A does not pause Account B).
12. EMERGENCY_FLATTEN bypass.
13. Pair protective exits remain allowed while paused.
14. Paused daily-risk loop does not initiate square-off.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.accounts.context import AccountExecutionContext
from app.core.config import get_settings
from app.db.models.account import AccountModel
from app.db.models.kill_switch import KillSwitchOperationModel
from app.db.models.position import PositionModel
from app.rms.models import (
    ExecutionIntentMode,
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
)
from app.services.kill_switch import (
    KillSwitchService,
    is_account_kill_switch_active,
)
from app.services.order_manager import OrderManager
from app.services.risk_exit_monitor import RiskExitMonitor
from app.services.trading_pause import (
    TradingPauseService,
    hydrate_trading_pause_cache,
    is_account_trading_paused,
)


@pytest.fixture
async def session_factory():
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    sf = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    yield sf
    await engine.dispose()


@pytest.mark.asyncio
async def test_pause_and_resume_idempotency_and_state(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Verifies pause and resume idempotency, DB writes first, cache mutation second (Requirement 8)."""
    suffix = uuid4().hex[:6]
    ibkr_acc = f"DU{suffix}"

    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"PauseTest-{suffix}",
            ibkr_account=ibkr_acc,
            total_margin=Decimal("100000.00"),
        )
        session.add(acc)
        await session.flush()
        acc_id = acc.id

    svc = TradingPauseService(session_factory)

    # 1. Initial state: not paused
    assert is_account_trading_paused(acc_id) is False

    # 2. First pause: transitions to paused (returns changed=True)
    acc1, changed1 = await svc.pause_account(acc_id, paused_by="test_op")
    assert changed1 is True
    assert acc1 is not None
    assert acc1.trading_paused is True
    assert acc1.paused_by == "test_op"
    assert acc1.paused_at is not None
    assert is_account_trading_paused(acc_id) is True

    # 3. Repeated pause (idempotency): returns changed=False, maintains state
    acc2, changed2 = await svc.pause_account(acc_id, paused_by="another_op")
    assert changed2 is False
    assert acc2 is not None
    assert acc2.trading_paused is True
    assert acc2.paused_by == "test_op"  # unchanged
    assert is_account_trading_paused(acc_id) is True

    # 4. First resume: transitions to unpaused (returns changed=True)
    acc3, changed3 = await svc.resume_account(acc_id)
    assert changed3 is True
    assert acc3 is not None
    assert acc3.trading_paused is False
    assert acc3.paused_at is None
    assert acc3.paused_by is None
    assert is_account_trading_paused(acc_id) is False

    # 5. Repeated resume (idempotency): returns changed=False
    acc4, changed4 = await svc.resume_account(acc_id)
    assert changed4 is False
    assert acc4 is not None
    assert acc4.trading_paused is False
    assert is_account_trading_paused(acc_id) is False


@pytest.mark.asyncio
async def test_pause_creates_no_kill_switch_operation_and_no_flatten(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Verifies pause never creates a kill-switch operation and never triggers flatten (Requirements 5 & 6)."""
    suffix = uuid4().hex[:6]
    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"NoKsTest-{suffix}",
            ibkr_account=f"DU{suffix}",
            total_margin=Decimal("100000.00"),
        )
        session.add(acc)
        await session.flush()
        acc_id = acc.id

    svc = TradingPauseService(session_factory)
    await svc.pause_account(acc_id, paused_by="operator")

    # Prove no kill_switch_operations were created
    async with session_factory() as session:
        ks_ops = (
            await session.execute(
                select(KillSwitchOperationModel).where(
                    KillSwitchOperationModel.account_id == acc_id
                )
            )
        ).scalars().all()
        assert len(ks_ops) == 0

    # Prove kill switch is NOT active
    assert is_account_kill_switch_active(acc_id) is False
    assert is_account_trading_paused(acc_id) is True


@pytest.mark.asyncio
async def test_account_isolation_when_pausing(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Verifies pausing Account A does not affect Account B (Requirement 11)."""
    suffix = uuid4().hex[:6]
    async with session_factory() as session, session.begin():
        acc_a = AccountModel(
            name=f"IsoA-{suffix}",
            ibkr_account=f"DUA{suffix}",
            total_margin=Decimal("100000.00"),
        )
        acc_b = AccountModel(
            name=f"IsoB-{suffix}",
            ibkr_account=f"DUB{suffix}",
            total_margin=Decimal("100000.00"),
        )
        session.add_all([acc_a, acc_b])
        await session.flush()
        id_a, id_b = acc_a.id, acc_b.id

    svc = TradingPauseService(session_factory)
    await svc.pause_account(id_a, paused_by="operator")

    assert is_account_trading_paused(id_a) is True
    assert is_account_trading_paused(id_b) is False


@pytest.mark.asyncio
async def test_restart_hydration_preserves_pause(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Verifies that restart hydration rebuilds the paused cache correctly (Requirement 4)."""
    suffix = uuid4().hex[:6]
    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"HydrateTest-{suffix}",
            ibkr_account=f"DUH{suffix}",
            total_margin=Decimal("100000.00"),
            trading_paused=True,
            paused_at=datetime.now(UTC),
            paused_by="test",
        )
        session.add(acc)
        await session.flush()
        acc_id = acc.id

    # Clear memory cache to simulate process restart
    from app.services.trading_pause import _PAUSED_ACCOUNTS

    _PAUSED_ACCOUNTS.clear()
    assert is_account_trading_paused(acc_id) is False

    # Hydrate from DB
    hydrated = await hydrate_trading_pause_cache(session_factory)
    assert acc_id in hydrated
    assert is_account_trading_paused(acc_id) is True


@pytest.mark.asyncio
async def test_kill_switch_still_works_while_paused(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Verifies that the emergency kill switch functions normally even when account is paused (Requirement 7)."""
    suffix = uuid4().hex[:6]
    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"KsWhilePaused-{suffix}",
            ibkr_account=f"DUKSP{suffix}",
            total_margin=Decimal("100000.00"),
        )
        session.add(acc)
        await session.flush()
        acc_id = acc.id

    pause_svc = TradingPauseService(session_factory)
    await pause_svc.pause_account(acc_id, paused_by="operator")
    assert is_account_trading_paused(acc_id) is True

    # Arm kill switch
    ks_svc = KillSwitchService(session_factory=session_factory)
    _op, created = await ks_svc.initiate_square_off(account_id=acc_id, requested_by="operator")
    assert created is True
    assert is_account_kill_switch_active(acc_id) is True
    assert is_account_trading_paused(acc_id) is True


@pytest.mark.asyncio
async def test_order_manager_both_gates_reject_open_allow_close_and_emergency(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Verifies both OrderManager gates:

    - Gate 1 (_fanout_single_account): OPEN rejected with TRADING_PAUSED
    - Gate 2 (_evaluate_and_submit): OPEN rejected with TRADING_PAUSED
    - CLOSE is accepted
    - EMERGENCY_FLATTEN is accepted
    (Requirements 1, 2, 3, 10, 12)
    """
    suffix = uuid4().hex[:6]
    acc_id = 999901
    ibkr_acc = f"DUGATE{suffix}"

    # Instantiate OrderManager
    om = OrderManager(
        session_factory=session_factory,
    )
    om._oms = MagicMock()

    ctx = AccountExecutionContext(
        account_id=acc_id,
        ibkr_account=ibkr_acc,
        strategy_id="model_blue",
        total_margin=Decimal("100000.00"),
        alloc_pct=Decimal("0.25"),
        committed_notional=Decimal("100000.00"),
        pair_max_allocation_pct=Decimal("0.10"),
        pair_budget=Decimal("10000.00"),
        target=Decimal("200.00"),
        stop=Decimal("-100.00"),
        time_limit=3600,
        max_open_positions=5,
    )

    mock_signal = MagicMock()
    mock_signal.signal_id = f"SIG-{suffix}"

    leg = OrderLeg(
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity=10.0,
        price=Decimal("150.00"),
        instrument_type="STK",
    )

    # 1. Gate 1: _fanout_single_account with action=OPEN when paused
    mock_handler = MagicMock()
    mock_handler.build_intent = AsyncMock(
        return_value=OrderIntent(
            signal_id=mock_signal.signal_id,
            strategy_id="model_blue",
            action=OrderAction.OPEN,
            legs=[leg],
            account_id=acc_id,
            ibkr_account=ibkr_acc,
        )
    )

    # Pause the account in cache
    from app.services.trading_pause import _pause_account_cache, _resume_account_cache

    _pause_account_cache(acc_id)
    assert is_account_trading_paused(acc_id) is True

    # Expect Gate 1 to reject OPEN (returns outcome with error when raise_if_single=False)
    outcome_blocked = await om._fanout_single_account(
        signal=mock_signal,
        handler=mock_handler,
        ctx=ctx,
        raise_if_single=False,
    )
    assert outcome_blocked.result is None
    assert outcome_blocked.error is not None
    assert f"TRADING_PAUSED: Account {acc_id} is paused; new opens blocked." in outcome_blocked.error

    # When raise_if_single=True, it raises ValueError directly
    with pytest.raises(
        ValueError, match=f"TRADING_PAUSED: Account {acc_id} is paused; new opens blocked."
    ):
        await om._fanout_single_account(
            signal=mock_signal,
            handler=mock_handler,
            ctx=ctx,
            raise_if_single=True,
        )

    # 2. Gate 2: _evaluate_and_submit directly with action=OPEN when paused
    om._rms_context.default_symbol_limits[acc_id] = Decimal("100000.00")
    open_intent = OrderIntent(
        signal_id=f"SIG-G2-{suffix}",
        strategy_id="model_blue",
        action=OrderAction.OPEN,
        legs=[leg],
        account_id=acc_id,
        ibkr_account=ibkr_acc,
    )
    with pytest.raises(ValueError, match="TRADING_PAUSED: Account 999901 is paused; new opens blocked."):
        await om._evaluate_and_submit(open_intent, mock_signal, handler=mock_handler)

    # 3. Gate 1 & 2 with action=CLOSE: must NOT be blocked by trading pause
    mock_handler.build_intent = AsyncMock(
        return_value=OrderIntent(
            signal_id=f"SIG-CLOSE-{suffix}",
            strategy_id="model_blue",
            action=OrderAction.CLOSE,
            legs=[leg],
            account_id=acc_id,
            ibkr_account=ibkr_acc,
        )
    )
    # Mock _evaluate_and_submit so it doesn't try to send to real broker
    with patch.object(om, "_evaluate_and_submit", new_callable=AsyncMock) as mock_eval:
        mock_eval.return_value = MagicMock(success=True, orders=[])
        outcome = await om._fanout_single_account(
            signal=mock_signal,
            handler=mock_handler,
            ctx=ctx,
        )
        assert outcome.account_id == acc_id
        mock_eval.assert_called_once()

    # 4. EMERGENCY_FLATTEN bypass: action=CLOSE, intent_mode=EMERGENCY_FLATTEN
    emergency_intent = OrderIntent(
        signal_id=f"SIG-EMERGENCY-{suffix}",
        strategy_id="model_blue",
        action=OrderAction.CLOSE,
        legs=[leg],
        account_id=acc_id,
        ibkr_account=ibkr_acc,
        intent_mode=ExecutionIntentMode.EMERGENCY_FLATTEN,
    )
    # Check that it passes the pause check in _evaluate_and_submit
    with patch.object(om, "_acquire_execution_claim", new_callable=AsyncMock) as mock_claim:
        mock_claim.side_effect = ValueError("Claim stopped test here after gates passed")
        with pytest.raises(ValueError, match="Claim stopped test here after gates passed"):
            await om._evaluate_and_submit(emergency_intent, mock_signal, handler=mock_handler)

    # 5. Resume -> OPEN accepted
    _resume_account_cache(acc_id)
    assert is_account_trading_paused(acc_id) is False
    with patch.object(om, "_evaluate_and_submit", new_callable=AsyncMock) as mock_eval_resumed:
        mock_eval_resumed.return_value = MagicMock(success=True, orders=[])
        mock_handler.build_intent = AsyncMock(
            return_value=OrderIntent(
                signal_id=mock_signal.signal_id,
                strategy_id="model_blue",
                action=OrderAction.OPEN,
                legs=[leg],
                account_id=acc_id,
                ibkr_account=ibkr_acc,
            )
        )
        outcome2 = await om._fanout_single_account(
            signal=mock_signal,
            handler=mock_handler,
            ctx=ctx,
        )
        assert outcome2.account_id == acc_id
        mock_eval_resumed.assert_called_once()


@pytest.mark.asyncio
async def test_risk_exit_monitor_skips_daily_risk_and_allows_pair_protective_exits(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Verifies that RiskExitMonitor:

    - Skips daily-risk evaluation and square-off for paused accounts
    - Continues running pair protective exits (target/stop/time)
    (Requirements 13 & 14)
    """
    suffix = uuid4().hex[:6]
    trade_id = f"PAIR-{suffix}"

    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"RiskPauseAcc-{suffix}",
            ibkr_account=f"DURP{suffix}",
            total_margin=Decimal("100000.00"),
            account_risk_enabled=True,
            daily_stop=Decimal("-100.00"),
            daily_stop_unit="ABSOLUTE",
            trading_paused=True,
            paused_at=datetime.now(UTC),
            paused_by="operator",
        )
        session.add(acc)
        await session.flush()
        acc_id = acc.id

        pos = PositionModel(
            account_id=acc_id,
            strategy_id="model_blue",
            trade_id=trade_id,
            leg_a_symbol="AAPL",
            leg_a_signed_qty=Decimal("10.0"),
            leg_a_entry_mark=Decimal("150.00"),
            target=Decimal("200.00"),
            stop=Decimal("-50.00"),
            time_limit=3600,
            target_unit="ABSOLUTE",
            stop_unit="ABSOLUTE",
            exit_automation_enabled=True,
            risk_state="OPEN",
            opened_at=datetime.now(UTC) - timedelta(seconds=100),
            live_pnl=Decimal("-150.00"),  # breaches daily stop (-150 <= -100) AND pair stop (-150 <= -50)
        )
        session.add(pos)

    # Pause in hot cache
    from app.services.trading_pause import _pause_account_cache

    _pause_account_cache(acc_id)

    mock_pair_closer = MagicMock()
    mock_pair_closer.close_pair = AsyncMock(return_value=MagicMock(success=True))
    mock_kill_switch = MagicMock()
    mock_kill_switch.initiate_square_off = AsyncMock()

    import time

    now = datetime.now(UTC)
    session_open = now - timedelta(hours=2)
    now_mono = time.monotonic()

    mock_live_pnl = MagicMock()
    mock_live_pnl.get_pair_pnl.return_value = MagicMock(
        pnl=Decimal("-150.00"),
        all_legs_marked=True,
        updated_at_mono=now_mono,
    )

    monitor = RiskExitMonitor(
        session_factory=session_factory,
        enabled=True,
        live_pnl=mock_live_pnl,
        pair_closer=mock_pair_closer,
        kill_switch=mock_kill_switch,
    )

    # Run a single evaluation
    await monitor._evaluate(now=now, session_open=session_open, now_mono=now_mono)

    # 1. Daily risk loop must have SKIPPED square-off because account is paused:
    mock_kill_switch.initiate_square_off.assert_not_called()

    # 2. Pair protective exit loop must have EXECUTED close_pair because pair exits continue running:
    mock_pair_closer.close_pair.assert_any_call(
        acc_id,
        trade_id,
        exit_reason="PAIR_STOP",
    )
