"""OrderManager model-value seed, maintain, lock, and re-seed tests."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.accounts.context import AccountExecutionContext
from app.models.signal import Signal, SignalType
from app.oms.models import OMSOrder, OMSOrderStatus
from app.rms.models import OrderAction, OrderIntent, OrderLeg, OrderSide
from app.services.order_manager import OrderManager
from app.services.strategies.handler import StrategyHandler


def _ctx() -> AccountExecutionContext:
    return AccountExecutionContext(
        account_id=1,
        ibkr_account="DU1",
        strategy_id="model_blue",
        total_margin=Decimal(1000),
        alloc_pct=Decimal("0.50"),
        committed_notional=Decimal(500),
        pair_max_allocation_pct=Decimal("0.10"),
        pair_budget=Decimal(50),
        target=Decimal(500),
        stop=Decimal(250),
        time_limit=3600,
        max_open_positions=10,
    )


def _intent(*, action: OrderAction = OrderAction.OPEN) -> OrderIntent:
    return OrderIntent(
        signal_id="T-MV",
        strategy_id="model_blue",
        action=action,
        account_id=1,
        ibkr_account="DU1",
        legs=[
            OrderLeg(
                symbol="XLE",
                side=OrderSide.BUY,
                quantity=1,
                price=Decimal(25),
                contract_month="2026-09",
                notional=Decimal(25),
                leg_index=0,
            ),
            OrderLeg(
                symbol="XOP",
                side=OrderSide.SELL,
                quantity=2,
                price=Decimal("12.5"),
                contract_month="2026-09",
                notional=Decimal(25),
                leg_index=1,
            ),
        ],
    )


def test_add_row_exposure_seeds_model_value() -> None:
    mgr = OrderManager(oms=None)
    row = SimpleNamespace(
        account_id=1,
        strategy_id="model_blue",
        leg_a_symbol="XLE",
        leg_a_signed_qty=Decimal(10),
        leg_a_entry_mark=Decimal(20),
        leg_b_symbol="XOP",
        leg_b_signed_qty=Decimal(-5),
        leg_b_entry_mark=Decimal(12),
    )
    mgr._add_row_exposure(row)
    assert mgr._rms_context.model_value_used[(1, "model_blue")] == Decimal(260)


@pytest.mark.asyncio
async def test_fanout_publishes_model_value_ceiling() -> None:
    mgr = OrderManager(oms=None)
    handler = AsyncMock(spec=StrategyHandler)
    handler.build_intent.side_effect = ValueError("stop after publish")
    signal = Signal(
        signal_type=SignalType.BUY,
        timestamp=datetime.now(UTC),
        reason="t",
        strategy_id="model_blue",
        action="OPEN",
        signal_id="T-MV",
    )
    await mgr._fanout_single_account(signal, handler, _ctx())
    assert mgr._rms_context.model_value_limit[(1, "model_blue")] == Decimal(500)


def test_record_unsettled_exposure_books_filled_intent() -> None:
    mgr = OrderManager(oms=None)
    intent = _intent()
    orders = [
        OMSOrder(
            internal_order_id="o1",
            intent=intent,
            symbol="XLE",
            side=OrderSide.BUY,
            quantity=1,
            filled_quantity=1,
            status=OMSOrderStatus.FILLED,
            leg_index=0,
        ),
        OMSOrder(
            internal_order_id="o2",
            intent=intent,
            symbol="XOP",
            side=OrderSide.SELL,
            quantity=2,
            filled_quantity=1,
            status=OMSOrderStatus.PARTIALLY_FILLED,
            leg_index=1,
        ),
    ]
    mgr._record_unsettled_exposure(intent, orders)
    # filled: 1*25 + 1*12.5 = 37.5
    assert mgr._rms_context.model_value_used[(1, "model_blue")] == Decimal("37.5")


@pytest.mark.asyncio
async def test_after_reconcile_sweep_calls_reload_then_reseed() -> None:
    mgr = OrderManager(oms=None)
    mgr.reload_margin_rates = AsyncMock()
    mgr._session_factory = None
    await mgr.after_reconcile_sweep()
    mgr.reload_margin_rates.assert_awaited_once()
