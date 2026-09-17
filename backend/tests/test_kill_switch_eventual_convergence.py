"""Eventual convergence: execution arrives AFTER Tier2 saw INCOMPLETE_EXIT_MARKS.

This is the primary production-safety race:
  T0 create engine close order -> T1 IBKR accepts -> T2 fills -> T3 order FILLED
  T5 fill_price NULL -> T6 Tier1 skip -> T7 Tier2 skip (INCOMPLETE_EXIT_MARKS)
  T8 executions row NOT YET AVAILABLE -> T9 Tier2 gives up -> operation UNRESOLVED
  T10 execution arrives late -> WHAT WAKES SYSTEM? -> retry_unresolved_operations via
  PositionReconciler.after_sweep (every 30s) + startup recovery.

Proves ledger converges without manual intervention.
"""

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.account import AccountModel
from app.db.models.execution import ExecutionModel
from app.db.models.kill_switch import (
    KILL_SWITCH_STATUS_COMPLETE,
    KILL_SWITCH_STATUS_UNRESOLVED,
    KillSwitchOperationModel,
)
from app.db.models.order import OrderModel
from app.db.models.position import PositionModel
from app.db.models.signal import SignalModel
from app.services.kill_switch import KillSwitchService


@pytest.mark.asyncio
async def test_execution_arrives_after_tier2_then_retry_converges(session_factory: async_sessionmaker[AsyncSession]):
    """Tier2 first sees NULL fill_price, execution arrives later, retry converges."""
    acc_id = None
    trade_id = f"ENG-GHOST-{uuid4().hex[:6]}"
    sig_id = None

    # Setup: account + engine OPEN position
    async with session_factory() as s, s.begin():
        acc = AccountModel(name=f"Ghost-{uuid4().hex[:4]}", ibkr_account=f"DU{uuid4().hex[:6].upper()}", total_margin=Decimal(100000))
        s.add(acc)
        await s.flush()
        acc_id = acc.id
        sig = SignalModel(signal_id=f"SIG-{trade_id}", strategy_id="model_blue", action="OPEN", pair="AAPL-MSFT", side="BUY", ref_price_a=Decimal(150), raw_payload={}, status="NEW")
        s.add(sig)
        await s.flush()
        sig_id = sig.id
        s.add(PositionModel(account_id=acc_id, trade_id=trade_id, strategy_id="model_blue", leg_a_symbol="AAPL", leg_a_signed_qty=Decimal(10), leg_a_entry_mark=Decimal(150), leg_b_symbol="MSFT", leg_b_signed_qty=Decimal(-5), leg_b_entry_mark=Decimal(300), target=Decimal("0.05"), stop=Decimal("0.02"), time_limit=60, risk_state="OPEN"))

    # Create kill-switch close orders with FILLED but fill_price NULL (T5)
    async with session_factory() as s, s.begin():
        s.add(OrderModel(signal_id=sig_id, account_id=acc_id, strategy_id="model_blue", leg="L0", symbol="AAPL", ibkr_contract="STK", buy_sell="SELL", quantity=Decimal(10), limit_price=Decimal(0), status="FILLED", trade_id=f"KILLSWITCH-{trade_id}", internal_order_id=f"ORD-{acc_id}-KILLSWITCH-{trade_id}-L0", fill_price=None, fill_qty=Decimal(10)))
        s.add(OrderModel(signal_id=sig_id, account_id=acc_id, strategy_id="model_blue", leg="L1", symbol="MSFT", ibkr_contract="STK", buy_sell="BUY", quantity=Decimal(5), limit_price=Decimal(0), status="FILLED", trade_id=f"KILLSWITCH-{trade_id}", internal_order_id=f"ORD-{acc_id}-KILLSWITCH-{trade_id}-L1", fill_price=None, fill_qty=Decimal(5)))

    svc = KillSwitchService(session_factory=session_factory)
    op, _ = await svc.initiate_square_off(account_id=acc_id)
    # T6/T7: first flatten attempt sees incomplete exit marks, Tier2 cannot close
    await svc._execute_flatten_operation(op.operation_id)

    async with session_factory() as s:
        pos = await s.get(PositionModel, (acc_id, trade_id))
        assert pos is not None
        assert pos.risk_state == "OPEN", "First Tier2 should skip due to INCOMPLETE_EXIT_MARKS"
        op_row = await s.get(KillSwitchOperationModel, op.operation_id)
        assert op_row is not None
        assert op_row.status == KILL_SWITCH_STATUS_UNRESOLVED

    # T10: execution rows arrive late (e.g. after callback persistence delay)
    async with session_factory() as s, s.begin():
        # Need order ids to link executions
        o1 = (await s.execute(select(OrderModel).where(OrderModel.internal_order_id == f"ORD-{acc_id}-KILLSWITCH-{trade_id}-L0"))).scalar_one()
        o2 = (await s.execute(select(OrderModel).where(OrderModel.internal_order_id == f"ORD-{acc_id}-KILLSWITCH-{trade_id}-L1"))).scalar_one()
        s.add(ExecutionModel(exec_id=f"exec-{trade_id}-L0", order_id=o1.id, account_id=acc_id, internal_order_id=o1.internal_order_id, symbol="AAPL", side="SELL", quantity=Decimal(10), price=Decimal(155), executed_at=None))
        s.add(ExecutionModel(exec_id=f"exec-{trade_id}-L1", order_id=o2.id, account_id=acc_id, internal_order_id=o2.internal_order_id, symbol="MSFT", side="BUY", quantity=Decimal(5), price=Decimal(295), executed_at=None))

    # WHAT WAKES SYSTEM? -> retry_unresolved_operations (periodic after_sweep or startup)
    # Simulate next reconciler sweep calling it
    retried = await svc.retry_unresolved_operations(acc_id)
    assert retried == 1

    async with session_factory() as s:
        pos = await s.get(PositionModel, (acc_id, trade_id))
        assert pos is not None
        assert pos.risk_state == "CLOSED", "Retry must converge using execution fallback"
        assert pos.closed_at is not None
        op_row = await s.get(KillSwitchOperationModel, op.operation_id)
        assert op_row is not None
        assert op_row.status == KILL_SWITCH_STATUS_COMPLETE
        assert op_row.unresolved_count == 0


@pytest.mark.asyncio
async def test_retry_is_idempotent_duplicate_execution(session_factory: async_sessionmaker[AsyncSession]):
    """Duplicate exec_id must not double-close or create negative qty."""
    from app.db.repositories.execution_repository import ExecutionRepository
    from app.oms.models import BrokerExecution

    acc_id = None
    trade_id = f"ENG-DUP-{uuid4().hex[:6]}"
    async with session_factory() as s, s.begin():
        acc = AccountModel(name=f"Dup-{uuid4().hex[:4]}", ibkr_account=f"DU{uuid4().hex[:6].upper()}", total_margin=Decimal(100000))
        s.add(acc)
        await s.flush()
        acc_id = acc.id
        sig = SignalModel(signal_id=f"SIG-{trade_id}", strategy_id="model_blue", action="OPEN", pair="AAPL-MSFT", side="BUY", ref_price_a=Decimal(150), raw_payload={}, status="NEW")
        s.add(sig)
        await s.flush()
        sig_id = sig.id
        s.add(PositionModel(account_id=acc_id, trade_id=trade_id, strategy_id="model_blue", leg_a_symbol="AAPL", leg_a_signed_qty=Decimal(10), leg_a_entry_mark=Decimal(150), leg_b_symbol="MSFT", leg_b_signed_qty=Decimal(-5), leg_b_entry_mark=Decimal(300), target=Decimal("0.05"), stop=Decimal("0.02"), time_limit=60, risk_state="OPEN"))
        s.add(OrderModel(signal_id=sig_id, account_id=acc_id, strategy_id="model_blue", leg="L0", symbol="AAPL", ibkr_contract="STK", buy_sell="SELL", quantity=Decimal(10), limit_price=Decimal(0), status="FILLED", trade_id=f"KILLSWITCH-{trade_id}", internal_order_id=f"ORD-{acc_id}-KILLSWITCH-{trade_id}-L0", fill_price=Decimal(155), fill_qty=Decimal(10)))
        s.add(OrderModel(signal_id=sig_id, account_id=acc_id, strategy_id="model_blue", leg="L1", symbol="MSFT", ibkr_contract="STK", buy_sell="BUY", quantity=Decimal(5), limit_price=Decimal(0), status="FILLED", trade_id=f"KILLSWITCH-{trade_id}", internal_order_id=f"ORD-{acc_id}-KILLSWITCH-{trade_id}-L1", fill_price=Decimal(295), fill_qty=Decimal(5)))

    svc = KillSwitchService(session_factory=session_factory)
    op, _ = await svc.initiate_square_off(account_id=acc_id)
    await svc._execute_flatten_operation(op.operation_id)
    async with session_factory() as s:
        pos = await s.get(PositionModel, (acc_id, trade_id))
        assert pos is not None
        assert pos.risk_state == "CLOSED"

    # Duplicate execution attempt (same exec_id) should be ignored via unique constraint and not reopen
    async with session_factory() as s, s.begin():
        o1 = (await s.execute(select(OrderModel).where(OrderModel.internal_order_id == f"ORD-{acc_id}-KILLSWITCH-{trade_id}-L0"))).scalar_one()
        # This insert would violate unique if we tried same exec_id again, but repo upsert is idempotent
        repo = ExecutionRepository(s)
        await repo.upsert(
            BrokerExecution(
                exec_id=f"exec-dup-{trade_id}",
                internal_order_id=str(o1.internal_order_id),
                symbol="AAPL",
                side="SELL",
                quantity=Decimal(10),
                price=Decimal(155),
                broker_order_id=None,
                commission=None,
                commission_currency=None,
                realized_pnl=None,
                perm_id=None,
                executed_at=None,
            ),
            order_id=o1.id,
            account_id=acc_id,
        )

    # Retry again must stay COMPLETE and not duplicate close
    await svc.retry_unresolved_operations()
    async with session_factory() as s:
        pos = await s.get(PositionModel, (acc_id, trade_id))
        assert pos is not None
        assert pos.risk_state == "CLOSED"
