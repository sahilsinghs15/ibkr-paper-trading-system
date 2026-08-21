"""Unit and integration tests for Production Kill Switch / Emergency Flatten architecture."""

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.account import AccountModel
from app.db.models.kill_switch import (
    KILL_SWITCH_STATUS_ACTIVATING,
)
from app.rms.checks.money_per_stock import MoneyPerStockCheck
from app.rms.checks.strategy import StrategyCheck
from app.rms.models import (
    ExecutionIntentMode,
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
    RMSContext,
    RMSOutcome,
)
from app.services.kill_switch import KillSwitchService, is_account_kill_switch_active


@pytest.fixture
async def session_factory():
    from app.db.session import AsyncSessionLocal, engine
    yield AsyncSessionLocal
    await engine.dispose()


@pytest.mark.asyncio
async def test_emergency_flatten_bypasses_entry_rms_checks():
    """Verify EMERGENCY_FLATTEN intent mode bypasses entry-only checks 3 and 8."""
    context = RMSContext(strategy_configs={})  # Empty strategy config map

    leg = OrderLeg(
        symbol="AAPL",
        side=OrderSide.SELL,
        quantity=100.0,
        price=Decimal("150.00"),
        contract_month="202612",
        leg_index=0,
    )

    emergency_intent = OrderIntent(
        signal_id="TEST-EMERGENCY-1",
        strategy_id="UNKNOWN_STRATEGY",
        action=OrderAction.CLOSE,
        legs=[leg],
        account_id=1,
        ibkr_account="DU12345",
        intent_mode=ExecutionIntentMode.EMERGENCY_FLATTEN,
    )

    # StrategyCheck (Check 3)
    strat_result = StrategyCheck().evaluate(emergency_intent, context)
    assert strat_result.outcome == RMSOutcome.PASS

    # MoneyPerStockCheck (Check 8)
    money_result = MoneyPerStockCheck().evaluate(emergency_intent, context)
    assert money_result.outcome == RMSOutcome.PASS


@pytest.mark.asyncio
async def test_kill_switch_idempotency_and_active_flag(session_factory: async_sessionmaker[AsyncSession]):
    """Verify repeated square-off calls return existing active operation and activate blocking flag."""
    test_id = uuid4().hex[:6]
    ibkr_acc = f"DU{test_id}"

    async with session_factory() as session, session.begin():
        acc = AccountModel(name="KillSwitchTestAcc", ibkr_account=ibkr_acc, total_margin=Decimal("100000.00"))
        session.add(acc)
        await session.flush()
        acc_id = acc.id

    svc = KillSwitchService(session_factory=session_factory)

    # 1st call: creates new operation
    op1, created1 = await svc.initiate_square_off(account_id=acc_id, requested_by="operator")
    assert created1 is True
    assert op1.status == KILL_SWITCH_STATUS_ACTIVATING
    assert is_account_kill_switch_active(acc_id) is True

    # 2nd call: returns existing active operation
    op2, created2 = await svc.initiate_square_off(account_id=acc_id, requested_by="operator")
    assert created2 is False
    assert op2.operation_id == op1.operation_id
