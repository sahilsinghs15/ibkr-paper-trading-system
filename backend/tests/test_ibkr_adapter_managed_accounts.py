"""Fail-closed ibkr_account validation before placeOrder."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.broker.ibkr.tws_client import TWSClient
from app.instruments.models import ResolvedInstrument
from app.oms.ibkr_adapter import IBKRExecutionAdapter
from app.oms.models import OMSOrder, OMSOrderStatus
from app.rms.models import OrderAction, OrderIntent, OrderLeg, OrderSide
from tests.ibkr_test_utils import DEFAULT_TEST_IBKR_ACCOUNT, wire_test_managed_accounts

_TS = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def _order(*, ibkr_account: str | None = DEFAULT_TEST_IBKR_ACCOUNT) -> OMSOrder:
    intent = OrderIntent(
        signal_id="T-MANAGED",
        strategy_id="synthetic_n_leg",
        action=OrderAction.OPEN,
        ibkr_account=ibkr_account,
        legs=[
            OrderLeg(
                symbol="XLE",
                side=OrderSide.BUY,
                quantity=10,
                price=Decimal("50"),
                contract_month="2026-09",
                instrument_type="STK",
                leg_index=0,
            )
        ],
        timestamp=_TS,
    )
    return OMSOrder(
        internal_order_id="int-managed-1",
        intent=intent,
        symbol="XLE",
        side=OrderSide.BUY,
        quantity=10,
        order_type="MARKET",
        leg_index=0,
        resolved=ResolvedInstrument(
            symbol="XLE",
            requested_instrument_type="STK",
            sec_type="STK",
            exchange="SMART",
            currency="USD",
            con_id=12345,
        ),
    )


def _adapter(*, managed: list[str] | None = None) -> IBKRExecutionAdapter:
    client = MagicMock(spec=TWSClient)
    client.is_connected.return_value = True
    client.next_order_id = 900
    client.get_request_type.return_value = "order"
    adapter = IBKRExecutionAdapter(client=client)
    adapter.is_connected = lambda: True  # type: ignore[method-assign]
    if managed is not None:
        adapter.set_managed_accounts(managed)
    else:
        wire_test_managed_accounts(adapter)
    return adapter


@pytest.mark.asyncio
async def test_missing_ibkr_account_rejected_before_place_order() -> None:
    adapter = _adapter()
    order = _order(ibkr_account=None)

    result = await adapter.submit_order(order)

    assert result.status == OMSOrderStatus.ERROR
    assert "MISSING_IBKR_ACCOUNT" in (result.error_message or "")
    adapter._client.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_managed_account_rejected() -> None:
    adapter = _adapter(managed=["DUTEST"])
    order = _order(ibkr_account="DUe7ebbd7a")

    result = await adapter.submit_order(order)

    assert result.status == OMSOrderStatus.ERROR
    assert "UNMANAGED_ACCOUNT" in (result.error_message or "")
    adapter._client.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_listed_account_reaches_place_order() -> None:
    adapter = _adapter(managed=["DUTEST"])

    def ack(order_id: int, contract: Any, ib_order: Any) -> None:
        adapter.on_order_status(order_id, "Submitted", 0.0, 10.0, 0.0, 0, 0, 0.0, 1, "", 0.0)

    adapter._client.placeOrder.side_effect = ack
    order = _order(ibkr_account="dutest")

    result = await adapter.submit_order(order)

    assert result.status != OMSOrderStatus.ERROR
    adapter._client.placeOrder.assert_called_once()


@pytest.mark.asyncio
async def test_empty_managed_set_while_connected_fails_closed() -> None:
    adapter = _adapter(managed=[])
    order = _order(ibkr_account="DUTEST")

    result = await adapter.submit_order(order)

    assert result.status == OMSOrderStatus.ERROR
    assert "UNMANAGED_ACCOUNT" in (result.error_message or "")
    adapter._client.placeOrder.assert_not_called()
