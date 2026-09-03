"""Reproductions for docs/review/BUGS-lifecycle.md findings 1, 2 and 3.

Every test in this file is expected to FAIL against the current code. Each one
asserts the invariant the review says is violated, so a fix flips it to green.
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.broker.ibkr.tws_client import TWSClient
from app.db.models.account import AccountModel
from app.db.models.order import OrderModel
from app.db.models.signal import SignalModel
from app.db.repositories.position_repository import (
    RISK_STATE_OPEN,
    PositionRepository,
)
from app.models.model_blue_trade import OpenModelBlueTrade, OpenModelBlueTradeLeg
from app.oms.ibkr_adapter import IBKRExecutionAdapter
from app.oms.models import OMSOrderStatus
from app.oms.oms_service import OMSService
from app.rms.models import (
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
    RMSOutcome,
    RMSResult,
)
from app.services.kill_switch import KillSwitchService
from tests.ibkr_test_utils import DEFAULT_TEST_IBKR_ACCOUNT, wire_test_managed_accounts


def _adapter() -> IBKRExecutionAdapter:
    client = MagicMock(spec=TWSClient)
    client.is_connected.return_value = True
    client.next_order_id = 100
    client.get_request_type.return_value = "order"
    client.placeOrder.side_effect = lambda *_args, **_kwargs: None
    adapter = IBKRExecutionAdapter(client=client)
    wire_test_managed_accounts(adapter)
    return adapter


def _intent(quantity: float) -> OrderIntent:
    return OrderIntent(
        signal_id=f"LIFECYCLE-{uuid4().hex[:6]}",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(
                symbol="EWA",
                side=OrderSide.BUY,
                quantity=quantity,
                price=Decimal("25.00"),
                contract_month="2026-09",
                instrument_type="STK",
            )
        ],
        timestamp=datetime.now(UTC),
        ibkr_account=DEFAULT_TEST_IBKR_ACCOUNT,
    )


def _pass_rms(intent: OrderIntent) -> RMSResult:
    return RMSResult(
        outcome=RMSOutcome.PASS,
        intent=intent,
        original_intent=intent,
        timestamp=datetime.now(UTC),
    )


# ── Finding 1 — P0 ────────────────────────────────────────────────
# backend/app/oms/ibkr_adapter.py:786, mutation at :814-831.
# on_exec_details writes order.status with no terminal guard, unlike
# _apply_mapped_status at :585. A fill that lands after the cancel confirmation
# therefore regresses a CANCELLED order to PARTIALLY_FILLED, and the basket
# coordinator has already computed compensation from the pre-fill quantity
# (coordinator.py:884), so those shares are never unwound.


@pytest.mark.asyncio
async def test_exec_details_after_cancel_does_not_regress_terminal_order() -> None:
    adapter = _adapter()
    oms = OMSService(adapter=adapter)
    intent = _intent(100.0)
    order = await oms.submit_one_leg(intent, _pass_rms(intent), 0)
    tws_id = int(order.ibkr_order_id)

    # Basket timed out, cancel was sent, broker confirmed the cancel with nothing filled.
    adapter.on_order_status(
        orderId=tws_id,
        status="Cancelled",
        filled=0.0,
        remaining=100.0,
        avgFillPrice=0.0,
        permId=555,
        parentId=0,
        lastFillPrice=0.0,
        clientId=1,
        whyHeld="",
        mktCapPrice=0.0,
    )
    assert order.status == OMSOrderStatus.CANCELLED
    assert order.filled_quantity == 0

    # Compensation has already run off filled_quantity == 0 by this point.
    # Now the execDetails for a fill that happened just before the cancel arrives.
    execution = SimpleNamespace(
        orderId=tws_id,
        execId=f"exec-{uuid4().hex[:8]}",
        shares=50.0,
        price=25.05,
        cumQty=50.0,
        avgPrice=25.05,
        side="BOT",
        permId=555,
    )
    adapter.on_exec_details(reqId=tws_id, contract=MagicMock(), execution=execution)

    # The order must not leave its terminal state, and the adapter must not
    # silently absorb 50 shares of exposure that nothing will compensate.
    assert order.status == OMSOrderStatus.CANCELLED, (
        f"terminal CANCELLED order regressed to {order.status.value}; "
        f"{order.filled_quantity} shares are filled at the broker with no "
        "compensation order and no non-terminal owner"
    )


# ── Finding 2 — P0 ────────────────────────────────────────────────
# backend/app/oms/ibkr_adapter.py:606-607 sets FILLED from the status string
# alone, with filled_quantity still 0. The order is then terminal, so the
# guard at :585 discards the real quantities when they arrive. openOrder
# structurally carries no fill quantity (:768 passes qty_filled=None).


@pytest.mark.asyncio
async def test_filled_status_without_quantity_does_not_block_real_fill() -> None:
    adapter = _adapter()
    oms = OMSService(adapter=adapter)
    intent = _intent(100.0)
    order = await oms.submit_one_leg(intent, _pass_rms(intent), 0)
    tws_id = int(order.ibkr_order_id)

    # openOrder arrives first, reporting a terminal status but no quantity.
    adapter.on_open_order(
        orderId=tws_id,
        contract=MagicMock(),
        order=SimpleNamespace(totalQuantity=100.0, lmtPrice=25.0),
        orderState=SimpleNamespace(status="Filled"),
    )

    # The authoritative quantities follow on orderStatus.
    adapter.on_order_status(
        orderId=tws_id,
        status="Filled",
        filled=100.0,
        remaining=0.0,
        avgFillPrice=25.02,
        permId=777,
        parentId=0,
        lastFillPrice=25.02,
        clientId=1,
        whyHeld="",
        mktCapPrice=0.0,
    )

    # _basket_complete (coordinator.py:586-591) and _open_trade_from_fills
    # (persistence.py:77) both read filled_quantity, so 0 here means a fully
    # filled leg is treated as unfilled.
    assert order.filled_quantity == 100.0, (
        f"order is FILLED but filled_quantity={order.filled_quantity}; the "
        "terminal guard discarded the real fill quantity, so the basket will "
        "treat a filled leg as unfilled and compensate nothing"
    )
    assert order.average_fill_price == Decimal("25.02")


# ── Finding 3 — P0 ────────────────────────────────────────────────
# backend/app/services/kill_switch.py:483-499. The close-order filter does not
# exclude is_compensation, and a compensation order for a KILLSWITCH intent
# carries "KILLSWITCH-" in its internal_order_id (oms_service.py:295-311 over
# the ":UNWIND:L0" signal id). A restored leg therefore counts as a closed leg.


@pytest.mark.asyncio
async def test_kill_switch_does_not_count_compensation_order_as_close_fill(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    test_id = uuid4().hex[:6]
    ibkr_acc = f"DU{test_id}"
    trade_id = f"MBG-AAPL-MSFT-{test_id}"

    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"LifecycleAcc{test_id}",
            ibkr_account=ibkr_acc,
            total_margin=Decimal("100000.00"),
        )
        session.add(acc)
        await session.flush()
        acc_id = acc.id

        trade = OpenModelBlueTrade(
            trade_id=trade_id,
            strategy_id="model_blue",
            direction=1,
            legs=(
                OpenModelBlueTradeLeg(
                    symbol="AAPL",
                    instrument_type="STK",
                    side=OrderSide.BUY,
                    quantity=Decimal("100.00"),
                    price=Decimal("150.00"),
                ),
                OpenModelBlueTradeLeg(
                    symbol="MSFT",
                    instrument_type="STK",
                    side=OrderSide.SELL,
                    quantity=Decimal("50.00"),
                    price=Decimal("300.00"),
                ),
            ),
        )
        await PositionRepository(session).open_trade(
            trade,
            account_id=acc_id,
            target=Decimal("0.05"),
            stop=Decimal("0.02"),
            time_limit=60,
        )

        sig = SignalModel(
            strategy_id="model_blue",
            signal_id=f"{trade_id}:KS",
            trade_id=trade_id,
            action="CLOSE",
            pair="AAPL:MSFT",
            side="SELL",
            ref_price_a=Decimal("150.00"),
            ref_price_b=Decimal("300.00"),
            raw_payload={"source": "test"},
            status="NEW",
        )
        session.add(sig)
        await session.flush()
        sig_id = sig.id

        # Leg A's emergency close filled.
        leg_a_close = OrderModel(
            signal_id=sig_id,
            account_id=acc_id,
            strategy_id="model_blue",
            leg="L0",
            symbol="AAPL",
            ibkr_contract="AAPL-STK-SMART-USD",
            buy_sell="SELL",
            quantity=Decimal("100.00"),
            limit_price=Decimal("0.00"),
            status="FILLED",
            trade_id=trade_id,
            internal_order_id=f"ORD-{acc_id}-KILLSWITCH-{trade_id}-aaa111-L0",
            fill_price=Decimal("155.00"),
            fill_qty=Decimal("100.00"),
            is_compensation=False,
        )
        # Leg B's emergency close did not fill, so the basket compensated by
        # re-buying leg A. That order is FILLED and is_compensation=True.
        leg_a_unwind = OrderModel(
            signal_id=sig_id,
            account_id=acc_id,
            strategy_id="model_blue",
            leg="L0",
            symbol="AAPL",
            ibkr_contract="AAPL-STK-SMART-USD",
            buy_sell="BUY",
            quantity=Decimal("100.00"),
            limit_price=Decimal("0.00"),
            status="FILLED",
            trade_id=trade_id,
            internal_order_id=f"ORD-{acc_id}-KILLSWITCH-{trade_id}-aaa111:UNWIND:L0",
            fill_price=Decimal("155.20"),
            fill_qty=Decimal("100.00"),
            is_compensation=True,
            compensation_of_internal_order_id=(
                f"ORD-{acc_id}-KILLSWITCH-{trade_id}-aaa111-L0"
            ),
        )
        leg_b_close = OrderModel(
            signal_id=sig_id,
            account_id=acc_id,
            strategy_id="model_blue",
            leg="L1",
            symbol="MSFT",
            ibkr_contract="MSFT-STK-SMART-USD",
            buy_sell="BUY",
            quantity=Decimal("50.00"),
            limit_price=Decimal("0.00"),
            status="CANCELLED",
            trade_id=trade_id,
            internal_order_id=f"ORD-{acc_id}-KILLSWITCH-{trade_id}-aaa111-L1",
            fill_qty=Decimal("0.00"),
            is_compensation=False,
        )
        session.add_all([leg_a_close, leg_a_unwind, leg_b_close])

    mock_baskets = MagicMock()
    mock_baskets._event = AsyncMock()
    mock_om = MagicMock()
    mock_om._baskets = mock_baskets

    svc = KillSwitchService(session_factory=session_factory, order_manager=mock_om)
    op, created = await svc.initiate_square_off(account_id=acc_id)
    assert created is True

    await svc._reconcile_and_finalize(op.operation_id, acc_id, [])

    async with session_factory() as session:
        row = await PositionRepository(session).get_by_trade_id(
            trade_id, account_id=acc_id
        )
        assert row is not None

    # Nothing was actually flattened: leg A was closed and immediately restored,
    # leg B was never closed. The broker still holds the whole pair.
    assert row.risk_state == RISK_STATE_OPEN, (
        "position marked CLOSED while the broker still holds both legs: the "
        "compensation order that restored leg A was counted as a second closed "
        f"leg (closed_at={row.closed_at}, realised_pnl={row.realised_pnl})"
    )


@pytest.mark.asyncio
async def test_disconnect_parks_waiters_without_resolving_error_as_terminal() -> None:
    adapter = _adapter()
    oms = OMSService(adapter=adapter)
    intent = _intent(100.0)
    order = await oms.submit_one_leg(intent, _pass_rms(intent), 0)
    wait_task = asyncio.create_task(
        adapter.wait_for_terminal_or_fill(order.internal_order_id, timeout=2.0)
    )
    await asyncio.sleep(0.02)
    adapter.on_connection_closed()
    assert order.status == OMSOrderStatus.ERROR
    fut, _loop = adapter._fill_futures[order.internal_order_id]
    assert not fut.done()
    wait_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await wait_task


def test_compensation_complete_empty_is_not_success() -> None:
    from app.oms.coordinator import BasketCoordinator

    coord = BasketCoordinator(OMSService(adapter=_adapter()))
    assert coord._compensation_complete([]) is False
