"""OrderManager bookkeeping behind the Cancel Exposure net-basis per-symbol limit.

Net exposure (BUY +, SELL -) must be maintained alongside the gross figure at
every booking point, return to zero when a pair closes, and the per-signal
account setting must decide which basis check 8 uses.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.models.account import AccountModel
from app.db.models.position import PositionModel
from app.rms.models import OrderAction, OrderIntent, OrderLeg, OrderSide
from app.services.order_manager import OrderManager


def _pair(side_a: OrderSide, side_b: OrderSide, action: OrderAction, account_id: int) -> OrderIntent:
    return OrderIntent(
        signal_id=f"SIG-{uuid.uuid4().hex[:6]}",
        strategy_id="model_blue",
        action=action,
        account_id=account_id,
        legs=[
            OrderLeg(symbol="AAPL", side=side_a, quantity=10, price=Decimal(150)),
            OrderLeg(symbol="EWC", side=side_b, quantity=100, price=Decimal(20)),
        ],
    )


async def test_rebuild_from_positions_sets_signed_net_exposure(session_factory):
    acc_id = 70_000_000 + uuid.uuid4().int % 1_000_000_000
    async with session_factory() as s, s.begin():
        s.add(AccountModel(id=acc_id, name="net", ibkr_account=f"DUN{acc_id}", total_margin=Decimal(100000)))
        await s.flush()
        for i, (qa, qb) in enumerate(((Decimal(10), Decimal(-100)), (Decimal(-4), Decimal(50)))):
            s.add(
                PositionModel(
                    account_id=acc_id,
                    trade_id=f"NET-{acc_id}-{i}",
                    strategy_id="model_blue",
                    leg_a_symbol="AAPL",
                    leg_a_signed_qty=qa,
                    leg_a_entry_mark=Decimal(150),
                    leg_b_symbol="EWC",
                    leg_b_signed_qty=qb,
                    leg_b_entry_mark=Decimal(20),
                    target=Decimal(500),
                    stop=Decimal(-250),
                    time_limit=3600,
                    risk_state="OPEN",
                )
            )
    om = OrderManager(session_factory=session_factory)
    await om.rebuild_rms_from_positions()
    ctx = om._rms_context
    # AAPL: +10*150 - 4*150 = +900 net ; gross 1500 + 600 = 2100
    assert ctx.symbol_net_exposures[(acc_id, "AAPL")] == Decimal(900)
    assert ctx.symbol_exposures[(acc_id, "AAPL")] == Decimal(2100)
    # EWC: -100*20 + 50*20 = -1000 net ; gross 3000
    assert ctx.symbol_net_exposures[(acc_id, "EWC")] == Decimal(-1000)
    assert ctx.symbol_exposures[(acc_id, "EWC")] == Decimal(3000)


async def test_open_then_close_nets_back_to_zero():
    om = OrderManager()
    handler = MagicMock()
    handler.after_submit = AsyncMock()
    acc = 42
    opening = _pair(OrderSide.BUY, OrderSide.SELL, OrderAction.OPEN, acc)
    await om._update_runtime_state(opening, MagicMock(), handler=handler, sized_from=MagicMock())
    net = om._rms_context.symbol_net_exposures
    assert net[(acc, "AAPL")] == Decimal(1500)
    assert net[(acc, "EWC")] == Decimal(-2000)

    # CLOSE legs carry the reversed side (as Model Blue builds them).
    closing = _pair(OrderSide.SELL, OrderSide.BUY, OrderAction.CLOSE, acc)
    await om._update_runtime_state(closing, MagicMock(), handler=handler, sized_from=MagicMock())
    assert net[(acc, "AAPL")] == Decimal(0)
    assert net[(acc, "EWC")] == Decimal(0)
    assert om._rms_context.symbol_exposures[(acc, "AAPL")] == Decimal(0)


async def test_opposite_pair_reduces_net_but_adds_gross():
    om = OrderManager()
    handler = MagicMock()
    handler.after_submit = AsyncMock()
    acc = 43
    await om._update_runtime_state(
        _pair(OrderSide.BUY, OrderSide.SELL, OrderAction.OPEN, acc), MagicMock(), handler=handler, sized_from=MagicMock()
    )
    await om._update_runtime_state(
        _pair(OrderSide.SELL, OrderSide.BUY, OrderAction.OPEN, acc), MagicMock(), handler=handler, sized_from=MagicMock()
    )
    assert om._rms_context.symbol_net_exposures[(acc, "AAPL")] == Decimal(0)
    assert om._rms_context.symbol_exposures[(acc, "AAPL")] == Decimal(3000)


@pytest.mark.parametrize("enabled", [True, False])
async def test_fanout_refreshes_account_cancel_exposure_setting(enabled):
    from app.accounts.context import AccountExecutionContext

    om = OrderManager()
    ctx = AccountExecutionContext(
        account_id=77,
        ibkr_account="DU77",
        strategy_id="model_blue",
        total_margin=Decimal(100000),
        alloc_pct=Decimal("0.5"),
        committed_notional=Decimal(50000),
        pair_max_allocation_pct=Decimal("0.1"),
        pair_budget=Decimal(5000),
        target=Decimal(500),
        stop=Decimal(-250),
        time_limit=3600,
        max_open_positions=5,
        cancel_exposure=enabled,
    )
    # Start from the opposite state to prove the refresh overwrites it.
    if enabled:
        om._rms_context.cancel_exposure_accounts.discard(77)
    else:
        om._rms_context.cancel_exposure_accounts.add(77)
    handler = MagicMock()
    handler.build_intent = AsyncMock(side_effect=ValueError("stop after routing"))
    signal = MagicMock()
    signal.signal_id = "SIG-FLAG"
    await om._fanout_single_account(signal, handler, ctx)
    assert (77 in om._rms_context.cancel_exposure_accounts) is enabled
