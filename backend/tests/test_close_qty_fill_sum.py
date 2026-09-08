"""Regression tests for CLOSE fill qty summation (cancel + retry)."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.oms.models import OMSOrder, OMSOrderStatus
from app.rms.models import OrderSide
from app.services.model_blue.parser import ModelBlueValidationError
from app.services.model_blue.persistence import (
    _filled_qty_by_symbol,
    assert_close_qty_matches_open,
)

_TRADE_ID = "MBG-CBOE:ARKQ-AMEX:BLOK-20260904T1230"


def _close_order(
    symbol: str,
    *,
    quantity: float,
    filled_quantity: float,
    status: OMSOrderStatus = OMSOrderStatus.FILLED,
) -> OMSOrder:
    side = OrderSide.BUY if symbol == "BLOK" else OrderSide.SELL
    return OMSOrder(
        internal_order_id=f"ORD-{symbol}-{quantity}-{filled_quantity}",
        intent=MagicMock(),
        symbol=symbol,
        side=side,
        quantity=quantity,
        filled_quantity=filled_quantity,
        status=status,
    )


def test_cancel_then_retry_sums_actual_fills_only() -> None:
    """ARKQ/BLOK incident: cancelled zero-fill must not count intended qty."""
    orders = [
        _close_order("ARKQ", quantity=9, filled_quantity=9),
        _close_order(
            "BLOK",
            quantity=20,
            filled_quantity=0,
            status=OMSOrderStatus.CANCELLED,
        ),
        _close_order("BLOK", quantity=20, filled_quantity=20),
    ]
    filled = _filled_qty_by_symbol(orders)
    assert filled["ARKQ"] == Decimal(9)
    assert filled["BLOK"] == Decimal(20)

    assert_close_qty_matches_open(
        trade_id=_TRADE_ID,
        leg_a_symbol="ARKQ",
        leg_a_signed_qty=Decimal(9),
        leg_b_symbol="BLOK",
        leg_b_signed_qty=Decimal(-20),
        filled_by_symbol=filled,
    )


def test_partial_cancel_then_remainder_retry() -> None:
    orders = [
        _close_order(
            "BLOK",
            quantity=20,
            filled_quantity=5,
            status=OMSOrderStatus.CANCELLED,
        ),
        _close_order("BLOK", quantity=15, filled_quantity=15),
    ]
    filled = _filled_qty_by_symbol(orders)
    assert filled["BLOK"] == Decimal(20)

    assert_close_qty_matches_open(
        trade_id=_TRADE_ID,
        leg_a_symbol="ARKQ",
        leg_a_signed_qty=Decimal(9),
        leg_b_symbol="BLOK",
        leg_b_signed_qty=Decimal(-20),
        filled_by_symbol={"ARKQ": Decimal(9), **filled},
    )


def test_short_fill_still_raises_close_qty_mismatch() -> None:
    orders = [_close_order("BLOK", quantity=20, filled_quantity=10)]
    filled = _filled_qty_by_symbol(orders)
    assert filled["BLOK"] == Decimal(10)

    with pytest.raises(ModelBlueValidationError, match="CLOSE_QTY_MISMATCH"):
        assert_close_qty_matches_open(
            trade_id=_TRADE_ID,
            leg_a_symbol="ARKQ",
            leg_a_signed_qty=Decimal(9),
            leg_b_symbol="BLOK",
            leg_b_signed_qty=Decimal(-20),
            filled_by_symbol={"ARKQ": Decimal(9), **filled},
        )


def test_fill_qty_used_when_filled_quantity_missing() -> None:
    order = SimpleNamespace(
        symbol="BLOK",
        is_compensation=False,
        fill_qty=20,
    )
    filled = _filled_qty_by_symbol([order])  # type: ignore[list-item]
    assert filled["BLOK"] == Decimal(20)


def test_close_fills_match_open_with_cancel_and_retry() -> None:
    from app.services.model_blue.persistence import close_fills_match_open

    orders = [
        _close_order("ARKQ", quantity=9, filled_quantity=9),
        _close_order(
            "BLOK",
            quantity=20,
            filled_quantity=0,
            status=OMSOrderStatus.CANCELLED,
        ),
        _close_order("BLOK", quantity=20, filled_quantity=20),
    ]
    assert close_fills_match_open(
        trade_id=_TRADE_ID,
        leg_a_symbol="ARKQ",
        leg_a_signed_qty=Decimal(9),
        leg_b_symbol="BLOK",
        leg_b_signed_qty=Decimal(-20),
        orders=orders,
    )
