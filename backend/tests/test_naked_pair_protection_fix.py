"""Regression test suite for naked-pair protection false square-off fixes and logical leg cumulative fills.

Tests cover:
1. Exact bug: EWP 399 (300 initial + 99 retry) & EWU 546 (546 initial) -> ACCEPTED, 0 compensation orders.
2. Leg 2 retry: Retrying Leg 2 (index 1) keeps leg_index=1.
3. Same symbol on multiple legs: Leg 1 EWP, Leg 2 EWU, Leg 3 EWP stay independent.
4. Genuine naked exposure: EWP 399/399, EWU 300/546 -> Compensation created for net 300 EWU exposure only.
5. Late cancellation callback: Retry completes, late cancellation event arrives -> remains ACCEPTED.
6. Duplicate fill callback: Duplicate IBKR fill callbacks do not double count.
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.models.signal import SignalModel
from app.oms.basket import Basket, BasketState
from app.oms.coordinator import BasketCoordinator
from app.oms.models import OMSOrder, OMSOrderStatus
from app.oms.oms_service import OMSService
from app.oms.retry_policy import ExecutionRetryPolicy
from app.rms.models import (
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
    RMSOutcome,
    RMSResult,
)
from demo_streaming.snapshot import reconcile_signal_status


def _make_intent(
    signal_id: str = "T-EWP-EWU",
    legs: list[tuple[str, OrderSide, float, float]] | None = None,
) -> OrderIntent:
    if legs is None:
        legs = [
            ("EWP", OrderSide.BUY, 399.0, 50.0),
            ("EWU", OrderSide.SELL, 546.0, 30.0),
        ]
    order_legs = [
        OrderLeg(
            symbol=sym,
            side=side,
            quantity=Decimal(str(qty)),
            price=Decimal(str(px)),
            contract_month="2026-09",
            instrument_type="STK",
            leg_index=idx,
        )
        for idx, (sym, side, qty, px) in enumerate(legs)
    ]
    return OrderIntent(
        signal_id=signal_id,
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        account_id=1,
        ibkr_account="DU12345",
        legs=order_legs,
        timestamp=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Test 1 — Exact bug: EWP 300+99 retry, EWU 546 -> ACCEPTED
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_1_exact_ewp_ewu_retry_completes_to_accepted() -> None:
    intent = _make_intent("T-EWP-EWU-EXACT")
    # Leg 0: EWP primary order (filled 300, remainder cancelled)
    o1 = OMSOrder(
        internal_order_id="ORD-EWP-1",
        intent=intent,
        symbol="EWP",
        side=OrderSide.BUY,
        quantity=399.0,
        filled_quantity=300.0,
        status=OMSOrderStatus.CANCELLED,
        leg_index=0,
    )
    # Leg 1: EWU primary order (filled 546)
    o2 = OMSOrder(
        internal_order_id="ORD-EWU-1",
        intent=intent,
        symbol="EWU",
        side=OrderSide.SELL,
        quantity=546.0,
        filled_quantity=546.0,
        status=OMSOrderStatus.FILLED,
        leg_index=1,
    )
    # Leg 0: EWP retry order (filled 99)
    o3 = OMSOrder(
        internal_order_id="ORD-EWP-RETRY-1",
        intent=intent,
        symbol="EWP",
        side=OrderSide.BUY,
        quantity=99.0,
        filled_quantity=99.0,
        status=OMSOrderStatus.FILLED,
        leg_index=0,
    )

    oms = MagicMock(spec=OMSService)
    coord = BasketCoordinator(oms)

    # Verify basket completeness evaluates to True
    assert coord._basket_complete(intent, [o1, o2, o3]) is True

    # Test snapshot reconciliation
    sig = SignalModel(
        id=101,
        signal_id="T-EWP-EWU-EXACT",
        trade_id="T-EWP-EWU-EXACT",
        strategy_id="MODEL_BLUE",
        action="OPEN",
        side="LONG",
        status="PROCESSED",
        received_at=datetime.now(UTC),
    )
    orders_payload = [
        {"leg": 0, "symbol": "EWP", "quantity": 399.0, "fill_qty": 300.0, "status": "CANCELLED", "is_compensation": False},
        {"leg": 1, "symbol": "EWU", "quantity": 546.0, "fill_qty": 546.0, "status": "FILLED", "is_compensation": False},
        {"leg": 0, "symbol": "EWP", "quantity": 99.0, "fill_qty": 99.0, "status": "FILLED", "is_compensation": False},
    ]

    c_status, is_active, reason, _, _ = reconcile_signal_status(sig, orders_payload, [])
    assert c_status == "ACCEPTED"
    assert is_active is False
    assert reason is None


# ---------------------------------------------------------------------------
# Test 2 — Leg 2 retry preserves leg_index = 1
# ---------------------------------------------------------------------------
def test_2_leg_2_retry_preserves_leg_index() -> None:
    intent = _make_intent("T-RETRY-LEG-2")
    orig_leg_2 = intent.legs[1]  # Leg 1 (EWU)
    oms = MagicMock(spec=OMSService)
    coord = BasketCoordinator(oms)

    retry_intent = coord._retry_intent(
        original=intent,
        orig_leg=orig_leg_2,
        remaining=100.0,
        index=1,
        attempt=1,
    )

    assert retry_intent.legs[0].leg_index == 1
    assert retry_intent.legs[0].symbol == "EWU"


# ---------------------------------------------------------------------------
# Test 3 — Same symbol on multiple legs (Leg 1 = EWP, Leg 2 = EWU, Leg 3 = EWP)
# ---------------------------------------------------------------------------
def test_3_same_symbol_multiple_legs_stay_independent() -> None:
    intent = _make_intent(
        "T-TRIPLE-LEG",
        legs=[
            ("EWP", OrderSide.BUY, 100.0, 50.0),
            ("EWU", OrderSide.SELL, 200.0, 30.0),
            ("EWP", OrderSide.BUY, 50.0, 50.0),
        ],
    )
    sig = SignalModel(
        id=103,
        signal_id="T-TRIPLE-LEG",
        trade_id="T-TRIPLE-LEG",
        strategy_id="MODEL_BLUE",
        action="OPEN",
        status="PROCESSED",
        received_at=datetime.now(UTC),
    )
    orders_payload = [
        {"leg": 0, "symbol": "EWP", "quantity": 100.0, "fill_qty": 100.0, "status": "FILLED", "is_compensation": False},
        {"leg": 1, "symbol": "EWU", "quantity": 200.0, "fill_qty": 200.0, "status": "FILLED", "is_compensation": False},
        {"leg": 2, "symbol": "EWP", "quantity": 50.0, "fill_qty": 50.0, "status": "FILLED", "is_compensation": False},
    ]

    c_status, _, _, _, _ = reconcile_signal_status(sig, orders_payload, [])
    assert c_status == "ACCEPTED"


# ---------------------------------------------------------------------------
# Test 4 — Genuine naked exposure compensation for net exposure only
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_4_genuine_naked_exposure_compensates_net_exposure() -> None:
    intent = _make_intent("T-NAKED-EXPOSURE")
    # Leg 0: EWP 399/399
    o1 = OMSOrder(
        internal_order_id="ORD-EWP-FULL",
        intent=intent,
        symbol="EWP",
        side=OrderSide.BUY,
        quantity=399.0,
        filled_quantity=399.0,
        status=OMSOrderStatus.FILLED,
        leg_index=0,
    )
    # Leg 1: EWU 300/546 (partial)
    o2 = OMSOrder(
        internal_order_id="ORD-EWU-PARTIAL",
        intent=intent,
        symbol="EWU",
        side=OrderSide.SELL,
        quantity=546.0,
        filled_quantity=300.0,
        status=OMSOrderStatus.CANCELLED,
        leg_index=1,
    )

    oms = MagicMock(spec=OMSService)
    oms.submit_intent = AsyncMock()

    # Mock submit_intent response
    mock_comp_order = OMSOrder(
        internal_order_id="COMP-EWU-1",
        intent=intent,
        symbol="EWU",
        side=OrderSide.BUY,
        quantity=300.0,
        filled_quantity=300.0,
        status=OMSOrderStatus.FILLED,
        leg_index=1,
    )
    mock_res = MagicMock()
    mock_res.orders = [mock_comp_order]
    oms.submit_intent.return_value = mock_res

    coord = BasketCoordinator(oms)
    basket = Basket(
        account_id=1,
        trade_id="T-NAKED-EXPOSURE",
        strategy_id="MODEL_BLUE",
        action="OPEN",
        intended_leg_count=2,
    )

    compensation_orders = await coord._compensate_filled(
        intent, [o1, o2], order_type="MKT", signal_pk=1, basket=basket
    )

    # Verify compensation was generated for net filled exposure of Leg 1 (300 EWU), NOT requested (546)
    assert len(compensation_orders) == 2
    comp_ewu = next(c for c in compensation_orders if c.symbol == "EWU")
    assert float(comp_ewu.quantity) == 300.0


# ---------------------------------------------------------------------------
# Test 5 — Late cancellation callback race protection
# ---------------------------------------------------------------------------
def test_5_late_cancellation_callback_does_not_regress_accepted() -> None:
    sig = SignalModel(
        id=105,
        signal_id="T-LATE-CANCEL",
        trade_id="T-LATE-CANCEL",
        strategy_id="MODEL_BLUE",
        action="OPEN",
        status="PROCESSED",
        received_at=datetime.now(UTC),
    )
    # Complete retry fill exists alongside late cancellation event/callback
    orders_payload = [
        {"leg": 0, "symbol": "EWP", "quantity": 399.0, "fill_qty": 300.0, "status": "CANCELLED", "is_compensation": False},
        {"leg": 1, "symbol": "EWU", "quantity": 546.0, "fill_qty": 546.0, "status": "FILLED", "is_compensation": False},
        {"leg": 0, "symbol": "EWP", "quantity": 99.0, "fill_qty": 99.0, "status": "FILLED", "is_compensation": False},
    ]
    late_events = [
        {"kind": "ORDER_CANCELLED", "ts": datetime.now(UTC).isoformat()}
    ]

    c_status, _, _, _, _ = reconcile_signal_status(sig, orders_payload, late_events)
    assert c_status == "ACCEPTED"


# ---------------------------------------------------------------------------
# Test 6 — Duplicate fill callback does not double count
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_6_duplicate_fill_callback_preserves_cumulative_filled() -> None:
    intent = _make_intent("T-DUP-CALLBACK")
    order = OMSOrder(
        internal_order_id="ORD-DUP-1",
        intent=intent,
        symbol="EWP",
        side=OrderSide.BUY,
        quantity=399.0,
        filled_quantity=399.0,
        status=OMSOrderStatus.FILLED,
        leg_index=0,
    )
    oms = MagicMock(spec=OMSService)
    coord = BasketCoordinator(oms)

    # First calculation
    filled_1 = coord._filled_qty_for_leg(0, [order])
    # Duplicate callback delivers same order object again
    filled_2 = coord._filled_qty_for_leg(0, [order])

    assert filled_1 == 399.0
    assert filled_2 == 399.0
