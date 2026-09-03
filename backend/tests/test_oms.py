"""Offline unit tests for Order Management System (OMS) and IBKR Execution Adapter."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.broker.ibkr.tws_client import TWSClient
from app.oms.ibkr_adapter import IBKRExecutionAdapter
from app.oms.models import (
    ExecutionTimestamps,
    OMSOrder,
    OMSOrderStatus,
)
from app.oms.oms_service import OMSService
from app.rms.models import (
    CheckResult,
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
    RMSOutcome,
    RMSResult,
)
from tests.ibkr_test_utils import DEFAULT_TEST_IBKR_ACCOUNT, wire_test_managed_accounts


@pytest.fixture
def sample_intent() -> OrderIntent:
    """Fixture providing a standard valid OrderIntent."""
    return OrderIntent(
        signal_id="SIG_TEST_001",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(
                symbol="EWA",
                side=OrderSide.BUY,
                quantity=1,
                price=Decimal("25.00"),
                contract_month="2026-09",
                instrument_type="STK",
            )
        ],
        timestamp=datetime.now(UTC),
        ibkr_account=DEFAULT_TEST_IBKR_ACCOUNT,
    )


@pytest.fixture
def pass_rms_result(sample_intent: OrderIntent) -> RMSResult:
    """Fixture providing an RMSResult with PASS outcome."""
    return RMSResult(
        outcome=RMSOutcome.PASS,
        intent=sample_intent,
        original_intent=sample_intent,
        check_results=[
            CheckResult(check_number=2, check_name="Duplicate", outcome=RMSOutcome.PASS)
        ],
        timestamp=datetime.now(UTC),
    )


@pytest.fixture
def reject_rms_result(sample_intent: OrderIntent) -> RMSResult:
    """Fixture providing an RMSResult with REJECT outcome."""
    return RMSResult(
        outcome=RMSOutcome.REJECT,
        intent=sample_intent,
        original_intent=sample_intent,
        check_number=3,
        reason="Unknown strategy ID",
        check_results=[
            CheckResult(
                check_number=3,
                check_name="Strategy",
                outcome=RMSOutcome.REJECT,
                reason="Unknown strategy ID",
            )
        ],
        timestamp=datetime.now(UTC),
    )


@pytest.fixture
def mock_tws_client() -> MagicMock:
    """Fixture providing a mocked TWSClient."""
    client = MagicMock(spec=TWSClient)
    client.is_connected.return_value = True
    client.next_order_id = 100
    client.get_request_type.return_value = "order"
    return client


@pytest.fixture
def mock_adapter() -> IBKRExecutionAdapter:
    """Fixture providing an IBKRExecutionAdapter backed by a mock TWSClient."""
    client = MagicMock(spec=TWSClient)
    client.is_connected.return_value = True
    client.next_order_id = 100
    client.get_request_type.return_value = "order"

    adapter = IBKRExecutionAdapter(client=client)
    wire_test_managed_accounts(adapter)

    # Replace placeOrder with side effect simulating instant submission response
    def fake_place_order(order_id: int, contract: Any, order: Any) -> None:
        pass

    client.placeOrder.side_effect = fake_place_order
    return adapter


# ── Test Suite ───────────────────────────────────────────────────


def test_order_lifecycle_enum_values() -> None:
    """Test 1: Verify internal order lifecycle status enum values."""
    assert OMSOrderStatus.PENDING.value == "PENDING"
    assert OMSOrderStatus.SUBMITTED.value == "SUBMITTED"
    assert OMSOrderStatus.PARTIALLY_FILLED.value == "PARTIALLY_FILLED"
    assert OMSOrderStatus.FILLED.value == "FILLED"
    assert OMSOrderStatus.CANCELLED.value == "CANCELLED"
    assert OMSOrderStatus.REJECTED.value == "REJECTED"
    assert OMSOrderStatus.ERROR.value == "ERROR"


@pytest.mark.asyncio
async def test_oms_accepts_rms_pass(
    mock_adapter: IBKRExecutionAdapter,
    sample_intent: OrderIntent,
    pass_rms_result: RMSResult,
) -> None:
    """Test 2: OMS accepts intent that passed RMS."""
    oms = OMSService(adapter=mock_adapter)
    res = await oms.submit_intent(intent=sample_intent, rms_result=pass_rms_result)

    assert res.success is True
    assert res.order.status == OMSOrderStatus.PENDING
    assert res.order.symbol == "EWA"
    assert res.order.quantity == 1
    assert res.order.ibkr_order_id == 100
    assert len(res.orders) == 1
    mock_adapter._client.placeOrder.assert_called_once()


@pytest.mark.asyncio
async def test_oms_submits_every_intent_leg(
    mock_adapter: IBKRExecutionAdapter,
) -> None:
    """OMS must submit both legs of a two-leg Model Blue intent independently."""
    intent = OrderIntent(
        signal_id="MBG-XLE-XOP-OPEN",
        strategy_id="model_blue",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(
                symbol="XLE",
                side=OrderSide.BUY,
                quantity=399.4248,
                price=Decimal("62.59"),
                contract_month="2026-09",
                instrument_type="STK",
            ),
            OrderLeg(
                symbol="XOP",
                side=OrderSide.SELL,
                quantity=93.0625,
                price=Decimal("183.34"),
                contract_month="2026-09",
                instrument_type="STK",
            ),
        ],
        timestamp=datetime.now(UTC),
        ibkr_account=DEFAULT_TEST_IBKR_ACCOUNT,
    )
    rms_result = RMSResult(
        outcome=RMSOutcome.PASS,
        intent=intent,
        original_intent=intent,
        timestamp=datetime.now(UTC),
    )
    oms = OMSService(adapter=mock_adapter)
    res = await oms.submit_intent(intent=intent, rms_result=rms_result)

    assert res.success is True
    assert len(res.orders) == 2
    assert [o.symbol for o in res.orders] == ["XLE", "XOP"]
    assert [o.side for o in res.orders] == [OrderSide.BUY, OrderSide.SELL]
    assert res.orders[0].parent_signal_id == "MBG-XLE-XOP-OPEN"
    assert res.orders[1].parent_signal_id == "MBG-XLE-XOP-OPEN"
    assert mock_adapter._client.placeOrder.call_count == 2
    submitted_symbols = [
        call.args[1].symbol for call in mock_adapter._client.placeOrder.call_args_list
    ]
    assert submitted_symbols == ["XLE", "XOP"]
    xle_ib = mock_adapter._client.placeOrder.call_args_list[0].args[2]
    xop_ib = mock_adapter._client.placeOrder.call_args_list[1].args[2]
    assert xle_ib.totalQuantity == 399.4248
    assert xop_ib.totalQuantity == 93.0625


@pytest.mark.asyncio
async def test_oms_rejects_non_pass_intent(
    mock_adapter: IBKRExecutionAdapter,
    sample_intent: OrderIntent,
    reject_rms_result: RMSResult,
) -> None:
    """Test 3: OMS rejects intent that failed RMS checks."""
    oms = OMSService(adapter=mock_adapter)
    res = await oms.submit_intent(intent=sample_intent, rms_result=reject_rms_result)

    assert res.success is False
    assert res.order.status == OMSOrderStatus.REJECTED
    assert "RMS check failed" in (res.order.error_message or "")


@pytest.mark.asyncio
async def test_oms_duplicate_submission_rejection(
    mock_adapter: IBKRExecutionAdapter,
    sample_intent: OrderIntent,
    pass_rms_result: RMSResult,
) -> None:
    """Test 11: OMS prevents duplicate submission for same signal_id."""
    oms = OMSService(adapter=mock_adapter)

    res1 = await oms.submit_intent(intent=sample_intent, rms_result=pass_rms_result)
    assert res1.success is True

    res2 = await oms.submit_intent(intent=sample_intent, rms_result=pass_rms_result)
    assert res2.success is False
    assert "Duplicate intent submission attempt" in (res2.error_message or "")


def test_ibkr_order_and_contract_conversion(mock_adapter: IBKRExecutionAdapter, sample_intent: OrderIntent) -> None:
    """Test 4: Verify internal order to IBKR Contract and IBOrder conversion."""
    order = OMSOrder(
        internal_order_id="ORD-101",
        intent=sample_intent,
        symbol="EWA",
        side=OrderSide.BUY,
        quantity=5,
        limit_price=Decimal("25.50"),
        order_type="LIMIT",
        resolved=__import__("app.instruments.resolver", fromlist=["resolve_leg"]).resolve_leg(
            symbol="EWA", instrument_type="STK"
        ),
    )

    contract = mock_adapter._build_ibkr_contract(order)
    assert contract.symbol == "EWA"
    assert contract.secType == "STK"
    assert contract.exchange == "SMART"
    assert contract.currency == "USD"

    ib_order = mock_adapter._build_ibkr_order(order)
    assert ib_order.action == "BUY"
    assert ib_order.totalQuantity == 5.0
    assert ib_order.orderType == "LMT"
    assert ib_order.lmtPrice == 25.50


@pytest.mark.asyncio
async def test_ibkr_order_status_handling(
    mock_adapter: IBKRExecutionAdapter,
    sample_intent: OrderIntent,
    pass_rms_result: RMSResult,
) -> None:
    """Test 5: Verify IBKR orderStatus callback processing."""
    oms = OMSService(adapter=mock_adapter)
    res = await oms.submit_intent(intent=sample_intent, rms_result=pass_rms_result)
    order = res.order
    tws_id = int(order.ibkr_order_id)  # type: ignore[arg-type]

    # Simulate IBKR orderStatus callback for full fill
    mock_adapter.on_order_status(
        orderId=tws_id,
        status="Filled",
        filled=1.0,
        remaining=0.0,
        avgFillPrice=25.05,
        permId=12345,
        parentId=0,
        lastFillPrice=25.05,
        clientId=1,
        whyHeld="",
        mktCapPrice=0.0,
    )

    updated_order = oms.get_order(order.internal_order_id)
    assert updated_order is not None
    assert updated_order.status == OMSOrderStatus.FILLED
    assert updated_order.filled_quantity == 1
    assert updated_order.remaining_quantity == 0
    assert updated_order.average_fill_price == Decimal("25.05")
    assert updated_order.timestamps.order_status_received_at is not None


@pytest.mark.asyncio
async def test_ibkr_exec_details_handling(
    mock_adapter: IBKRExecutionAdapter,
    sample_intent: OrderIntent,
    pass_rms_result: RMSResult,
) -> None:
    """Test 6: Verify IBKR execDetails callback processing."""
    oms = OMSService(adapter=mock_adapter)
    res = await oms.submit_intent(intent=sample_intent, rms_result=pass_rms_result)
    order = res.order
    tws_id = int(order.ibkr_order_id)  # type: ignore[arg-type]

    # Mock Execution object from IBKR API
    execution = MagicMock()
    execution.orderId = tws_id
    execution.shares = 1.0
    execution.price = 25.10
    execution.cumQty = 1.0
    execution.avgPrice = 25.10

    mock_adapter.on_exec_details(reqId=tws_id, contract=MagicMock(), execution=execution)

    updated_order = oms.get_order(order.internal_order_id)
    assert updated_order is not None
    assert updated_order.status == OMSOrderStatus.FILLED
    assert updated_order.filled_quantity == 1
    assert updated_order.last_fill_price == Decimal("25.1")
    assert updated_order.average_fill_price == Decimal("25.1")
    assert updated_order.timestamps.execution_received_at is not None


@pytest.mark.asyncio
async def test_partial_fill_handling(
    mock_adapter: IBKRExecutionAdapter,
    pass_rms_result: RMSResult,
) -> None:
    """Test 7: Verify partial fill status transition and quantities."""
    intent = OrderIntent(
        signal_id="SIG_MULTI_001",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(
                symbol="EWA",
                side=OrderSide.BUY,
                quantity=10,
                price=Decimal("25.00"),
                contract_month="2026-09",
                instrument_type="STK",
            )
        ],
        timestamp=datetime.now(UTC),
        ibkr_account=DEFAULT_TEST_IBKR_ACCOUNT,
    )
    pass_rms = RMSResult(
        outcome=RMSOutcome.PASS,
        intent=intent,
        original_intent=intent,
        timestamp=datetime.now(UTC),
    )

    oms = OMSService(adapter=mock_adapter)
    res = await oms.submit_intent(intent=intent, rms_result=pass_rms)
    order = res.order
    tws_id = int(order.ibkr_order_id)  # type: ignore[arg-type]

    # First partial fill callback: 4 out of 10 filled
    mock_adapter.on_order_status(
        orderId=tws_id,
        status="Submitted",
        filled=4.0,
        remaining=6.0,
        avgFillPrice=24.95,
        permId=123,
        parentId=0,
        lastFillPrice=24.95,
        clientId=1,
        whyHeld="",
        mktCapPrice=0.0,
    )

    p1_order = oms.get_order(order.internal_order_id)
    assert p1_order is not None
    assert p1_order.status == OMSOrderStatus.PARTIALLY_FILLED
    assert p1_order.filled_quantity == 4
    assert p1_order.remaining_quantity == 6

    # Second callback: remaining 6 filled
    mock_adapter.on_order_status(
        orderId=tws_id,
        status="Filled",
        filled=10.0,
        remaining=0.0,
        avgFillPrice=24.98,
        permId=123,
        parentId=0,
        lastFillPrice=25.00,
        clientId=1,
        whyHeld="",
        mktCapPrice=0.0,
    )

    p2_order = oms.get_order(order.internal_order_id)
    assert p2_order is not None
    assert p2_order.status == OMSOrderStatus.FILLED
    assert p2_order.filled_quantity == 10
    assert p2_order.remaining_quantity == 0


@pytest.mark.asyncio
async def test_broker_rejection_handling(
    mock_adapter: IBKRExecutionAdapter,
    sample_intent: OrderIntent,
    pass_rms_result: RMSResult,
) -> None:
    """Test 8: Verify IBKR error/rejection callback handling."""
    oms = OMSService(adapter=mock_adapter)
    res = await oms.submit_intent(intent=sample_intent, rms_result=pass_rms_result)
    order = res.order
    tws_id = int(order.ibkr_order_id)  # type: ignore[arg-type]

    # Simulate IBKR Error 201 (Order rejected)
    mock_adapter.on_error(reqId=tws_id, errorCode=201, errorString="Order rejected by system")

    rej_order = oms.get_order(order.internal_order_id)
    assert rej_order is not None
    assert rej_order.status == OMSOrderStatus.REJECTED
    assert "Order rejected" in (rej_order.error_message or "")


@pytest.mark.asyncio
async def test_fractional_share_api_error_is_rejected(
    mock_adapter: IBKRExecutionAdapter,
    sample_intent: OrderIntent,
    pass_rms_result: RMSResult,
) -> None:
    oms = OMSService(adapter=mock_adapter)
    res = await oms.submit_intent(intent=sample_intent, rms_result=pass_rms_result)
    order = res.order
    tws_id = int(order.ibkr_order_id)  # type: ignore[arg-type]
    mock_adapter.on_error(
        reqId=tws_id,
        errorCode=10243,
        errorString="Fractional-sized order cannot be placed via API.",
    )
    rej = oms.get_order(order.internal_order_id)
    assert rej is not None
    assert rej.status == OMSOrderStatus.REJECTED
    assert "10243" in (rej.error_message or "")


@pytest.mark.asyncio
async def test_10243_unblocks_wait_without_timeout(
    mock_adapter: IBKRExecutionAdapter,
    sample_intent: OrderIntent,
    pass_rms_result: RMSResult,
) -> None:
    oms = OMSService(adapter=mock_adapter)
    res = await oms.submit_intent(intent=sample_intent, rms_result=pass_rms_result)
    order = res.order
    tws_id = int(order.ibkr_order_id)  # type: ignore[arg-type]
    mock_adapter.on_error(
        reqId=tws_id,
        errorCode=10243,
        errorString="Fractional-sized order cannot be placed via API.",
    )
    waited = await mock_adapter.wait_for_terminal_or_fill(
        order.internal_order_id, timeout=0.05
    )
    assert waited.status == OMSOrderStatus.REJECTED


def test_timestamp_collection_and_durations() -> None:
    """Test 9: Verify timestamp boundary collection and millisecond duration calculations."""
    t0 = datetime(2026, 8, 14, 10, 0, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 8, 14, 10, 0, 0, 10000, tzinfo=UTC)   # +10 ms
    t2 = datetime(2026, 8, 14, 10, 0, 0, 20000, tzinfo=UTC)   # +20 ms
    t3 = datetime(2026, 8, 14, 10, 0, 0, 50000, tzinfo=UTC)   # +50 ms
    t4 = datetime(2026, 8, 14, 10, 0, 0, 150000, tzinfo=UTC)  # +150 ms

    timestamps = ExecutionTimestamps(
        intent_created_at=t0,
        rms_started_at=t0,
        rms_completed_at=t1,
        oms_received_at=t1,
        ibkr_submit_started_at=t2,
        ibkr_submit_completed_at=t3,
        execution_received_at=t4,
    )

    assert pytest.approx(timestamps.rms_latency_ms, 0.01) == 10.0
    assert pytest.approx(timestamps.oms_latency_ms, 0.01) == 10.0
    assert pytest.approx(timestamps.ibkr_submit_latency_ms, 0.01) == 30.0
    assert pytest.approx(timestamps.submit_to_fill_ms, 0.01) == 100.0
    assert pytest.approx(timestamps.total_intent_to_fill_ms, 0.01) == 150.0


@pytest.mark.asyncio
async def test_connection_failure_handling(
    sample_intent: OrderIntent,
    pass_rms_result: RMSResult,
) -> None:
    """Test 10: Verify handling when TWS connection is unavailable."""
    client = MagicMock(spec=TWSClient)
    client.is_connected.return_value = False
    adapter = IBKRExecutionAdapter(client=client)

    oms = OMSService(adapter=adapter)
    res = await oms.submit_intent(intent=sample_intent, rms_result=pass_rms_result)

    assert res.success is False
    assert res.order.status == OMSOrderStatus.ERROR
    assert "TWS is not connected" in (res.order.error_message or "")


@pytest.mark.asyncio
async def test_wait_for_terminal_or_fill_timeout(
    mock_adapter: IBKRExecutionAdapter,
    sample_intent: OrderIntent,
    pass_rms_result: RMSResult,
) -> None:
    """Test 12: Verify wait_for_terminal_or_fill returns working order when fill timeout occurs."""
    oms = OMSService(adapter=mock_adapter)
    res = await oms.submit_intent(intent=sample_intent, rms_result=pass_rms_result)
    order = res.order

    working_order = await mock_adapter.wait_for_terminal_or_fill(order.internal_order_id, timeout=0.05)
    assert working_order.internal_order_id == order.internal_order_id
    assert working_order.status == OMSOrderStatus.PENDING


def _fire_order_status(adapter: IBKRExecutionAdapter, tws_id: int, status: str, **kwargs: Any) -> None:
    adapter.on_order_status(
        orderId=tws_id,
        status=status,
        filled=kwargs.get("filled", 0.0),
        remaining=kwargs.get("remaining", 1.0),
        avgFillPrice=kwargs.get("avgFillPrice", 0.0),
        permId=1,
        parentId=0,
        lastFillPrice=kwargs.get("lastFillPrice", 0.0),
        clientId=1,
        whyHeld="",
        mktCapPrice=0.0,
    )


@pytest.mark.asyncio
async def test_place_order_does_not_claim_submitted(
    mock_adapter: IBKRExecutionAdapter,
    sample_intent: OrderIntent,
    pass_rms_result: RMSResult,
) -> None:
    oms = OMSService(adapter=mock_adapter)
    res = await oms.submit_intent(intent=sample_intent, rms_result=pass_rms_result)
    mock_adapter._client.placeOrder.assert_called_once()
    assert res.order.status == OMSOrderStatus.PENDING
    assert res.order.status is not OMSOrderStatus.SUBMITTED


@pytest.mark.asyncio
async def test_broker_status_pending_submit_and_presubmitted(
    mock_adapter: IBKRExecutionAdapter,
    sample_intent: OrderIntent,
    pass_rms_result: RMSResult,
) -> None:
    oms = OMSService(adapter=mock_adapter)
    order = (await oms.submit_intent(intent=sample_intent, rms_result=pass_rms_result)).order
    tws_id = int(order.ibkr_order_id)  # type: ignore[arg-type]

    _fire_order_status(mock_adapter, tws_id, "PendingSubmit")
    assert oms.get_order(order.internal_order_id).status == OMSOrderStatus.PENDING  # type: ignore[union-attr]

    _fire_order_status(mock_adapter, tws_id, "PreSubmitted")
    assert oms.get_order(order.internal_order_id).status == OMSOrderStatus.PENDING  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_broker_status_submitted(
    mock_adapter: IBKRExecutionAdapter,
    sample_intent: OrderIntent,
    pass_rms_result: RMSResult,
) -> None:
    oms = OMSService(adapter=mock_adapter)
    order = (await oms.submit_intent(intent=sample_intent, rms_result=pass_rms_result)).order
    tws_id = int(order.ibkr_order_id)  # type: ignore[arg-type]
    _fire_order_status(mock_adapter, tws_id, "Submitted")
    assert oms.get_order(order.internal_order_id).status == OMSOrderStatus.SUBMITTED  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_broker_status_partially_filled_string(
    mock_adapter: IBKRExecutionAdapter,
    sample_intent: OrderIntent,
    pass_rms_result: RMSResult,
) -> None:
    oms = OMSService(adapter=mock_adapter)
    order = (await oms.submit_intent(intent=sample_intent, rms_result=pass_rms_result)).order
    tws_id = int(order.ibkr_order_id)  # type: ignore[arg-type]
    _fire_order_status(mock_adapter, tws_id, "PartiallyFilled", filled=0.4, remaining=0.6)
    assert oms.get_order(order.internal_order_id).status == OMSOrderStatus.PARTIALLY_FILLED  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_broker_status_cancelled_and_rejected(
    mock_adapter: IBKRExecutionAdapter,
    sample_intent: OrderIntent,
    pass_rms_result: RMSResult,
) -> None:
    oms = OMSService(adapter=mock_adapter)
    cancelled = (await oms.submit_intent(intent=sample_intent, rms_result=pass_rms_result)).order
    tws_id = int(cancelled.ibkr_order_id)  # type: ignore[arg-type]
    _fire_order_status(mock_adapter, tws_id, "Cancelled")
    assert oms.get_order(cancelled.internal_order_id).status == OMSOrderStatus.CANCELLED  # type: ignore[union-attr]

    intent2 = OrderIntent(
        signal_id="SIG_TEST_002",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=sample_intent.legs,
        timestamp=datetime.now(UTC),
        ibkr_account=DEFAULT_TEST_IBKR_ACCOUNT,
    )
    rms2 = RMSResult(
        outcome=RMSOutcome.PASS,
        intent=intent2,
        original_intent=intent2,
        timestamp=datetime.now(UTC),
    )
    rejected = (await oms.submit_intent(intent=intent2, rms_result=rms2)).order
    tws_id2 = int(rejected.ibkr_order_id)  # type: ignore[arg-type]
    _fire_order_status(mock_adapter, tws_id2, "Inactive")
    assert oms.get_order(rejected.internal_order_id).status == OMSOrderStatus.REJECTED  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_api_error_321_is_error(
    mock_adapter: IBKRExecutionAdapter,
    sample_intent: OrderIntent,
    pass_rms_result: RMSResult,
) -> None:
    oms = OMSService(adapter=mock_adapter)
    order = (await oms.submit_intent(intent=sample_intent, rms_result=pass_rms_result)).order
    tws_id = int(order.ibkr_order_id)  # type: ignore[arg-type]
    mock_adapter._client.get_request_type.return_value = "order"
    mock_adapter.on_error(reqId=tws_id, errorCode=321, errorString="Error 321: read-only API")
    stored = oms.get_order(order.internal_order_id)
    assert stored is not None
    assert stored.status == OMSOrderStatus.ERROR
    assert "321" in (stored.error_message or "")


@pytest.mark.asyncio
async def test_on_open_order_updates_existing_oms_order(
    mock_adapter: IBKRExecutionAdapter,
    sample_intent: OrderIntent,
    pass_rms_result: RMSResult,
) -> None:
    oms = OMSService(adapter=mock_adapter)
    order = (await oms.submit_intent(intent=sample_intent, rms_result=pass_rms_result)).order
    tws_id = int(order.ibkr_order_id)  # type: ignore[arg-type]
    before_count = len(oms.get_all_orders())
    before_adapter = len(mock_adapter._orders_by_tws_id)

    mock_adapter.on_open_order(
        tws_id,
        MagicMock(),
        MagicMock(),
        SimpleNamespace(status="Submitted"),
    )

    stored = oms.get_order(order.internal_order_id)
    assert stored is not None
    assert stored is order
    assert stored.status == OMSOrderStatus.SUBMITTED
    assert len(oms.get_all_orders()) == before_count
    assert len(mock_adapter._orders_by_tws_id) == before_adapter


@pytest.mark.asyncio
async def test_callback_during_place_order_finds_registered_order(
    sample_intent: OrderIntent,
    pass_rms_result: RMSResult,
) -> None:
    """openOrder/orderStatus during placeOrder must locate the pre-registered OMSOrder."""
    client = MagicMock(spec=TWSClient)
    client.is_connected.return_value = True
    client.next_order_id = 500
    client.get_request_type.return_value = "order"
    adapter = IBKRExecutionAdapter(client=client)
    wire_test_managed_accounts(adapter)

    def immediate_broker_ack(order_id: int, contract: Any, ib_order: Any) -> None:
        assert order_id in adapter._orders_by_tws_id
        adapter.on_open_order(
            order_id,
            contract,
            ib_order,
            SimpleNamespace(status="Submitted"),
        )
        adapter.on_order_status(
            orderId=order_id,
            status="Submitted",
            filled=0.0,
            remaining=1.0,
            avgFillPrice=0.0,
            permId=1,
            parentId=0,
            lastFillPrice=0.0,
            clientId=1,
            whyHeld="",
            mktCapPrice=0.0,
        )

    client.placeOrder.side_effect = immediate_broker_ack
    oms = OMSService(adapter=adapter)
    res = await oms.submit_intent(intent=sample_intent, rms_result=pass_rms_result)

    assert res.success is True
    assert res.order.status == OMSOrderStatus.SUBMITTED
    assert res.order.ibkr_order_id == 500
    assert adapter._orders_by_tws_id[500] is res.order
    assert len(adapter._orders_by_tws_id) == 1


@pytest.mark.asyncio
async def test_10243_does_not_overwrite_filled(
    mock_adapter: IBKRExecutionAdapter,
    sample_intent: OrderIntent,
    pass_rms_result: RMSResult,
) -> None:
    oms = OMSService(adapter=mock_adapter)
    res = await oms.submit_intent(intent=sample_intent, rms_result=pass_rms_result)
    tws_id = int(res.order.ibkr_order_id)  # type: ignore[arg-type]
    mock_adapter.on_order_status(
        tws_id, "Filled", 1.0, 0.0, 25.0, 0, 0, 25.0, 1, "", 0.0
    )
    assert oms.get_order(res.order.internal_order_id).status == OMSOrderStatus.FILLED
    mock_adapter.on_error(
        reqId=tws_id,
        errorCode=10243,
        errorString="Fractional-sized order cannot be placed via API.",
    )
    filled = oms.get_order(res.order.internal_order_id)
    assert filled is not None
    assert filled.status == OMSOrderStatus.FILLED


@pytest.mark.asyncio
async def test_399_and_2109_are_non_terminal_warnings(
    mock_adapter: IBKRExecutionAdapter,
    sample_intent: OrderIntent,
    pass_rms_result: RMSResult,
) -> None:
    oms = OMSService(adapter=mock_adapter)
    res = await oms.submit_intent(intent=sample_intent, rms_result=pass_rms_result)
    tws_id = int(res.order.ibkr_order_id)  # type: ignore[arg-type]
    mock_adapter._client.get_request_type.return_value = "order"
    mock_adapter.on_error(
        reqId=tws_id,
        errorCode=399,
        errorString="Order Message: Warning: held until RTH",
    )
    pending = oms.get_order(res.order.internal_order_id)
    assert pending is not None
    assert pending.status == OMSOrderStatus.PENDING
    mock_adapter.on_error(
        reqId=tws_id,
        errorCode=2109,
        errorString='Order Event Warning: Attribute "Outside Regular Trading Hours" is ignored. PlaceOrder is now being processed.',
    )
    still = oms.get_order(res.order.internal_order_id)
    assert still is not None
    assert still.status == OMSOrderStatus.PENDING
    mock_adapter.on_order_status(
        tws_id, "Submitted", 0.0, 1.0, 0.0, 0, 0, 0.0, 1, "", 0.0
    )
    assert oms.get_order(res.order.internal_order_id).status == OMSOrderStatus.SUBMITTED


@pytest.mark.asyncio
async def test_201_is_rejected_and_202_is_cancelled(
    mock_adapter: IBKRExecutionAdapter,
    sample_intent: OrderIntent,
    pass_rms_result: RMSResult,
) -> None:
    oms = OMSService(adapter=mock_adapter)
    first = (await oms.submit_intent(intent=sample_intent, rms_result=pass_rms_result)).order
    tws_id = int(first.ibkr_order_id)  # type: ignore[arg-type]
    mock_adapter._client.get_request_type.return_value = "order"
    mock_adapter.on_error(reqId=tws_id, errorCode=201, errorString="Order rejected")
    assert oms.get_order(first.internal_order_id).status == OMSOrderStatus.REJECTED

    intent2 = OrderIntent(
        signal_id="SIG_202",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=sample_intent.legs,
        timestamp=datetime.now(UTC),
        ibkr_account=DEFAULT_TEST_IBKR_ACCOUNT,
    )
    rms2 = RMSResult(
        outcome=RMSOutcome.PASS,
        intent=intent2,
        original_intent=intent2,
        timestamp=datetime.now(UTC),
    )
    second = (await oms.submit_intent(intent=intent2, rms_result=rms2)).order
    tws2 = int(second.ibkr_order_id)  # type: ignore[arg-type]
    mock_adapter.on_error(reqId=tws2, errorCode=202, errorString="Order Canceled")
    assert oms.get_order(second.internal_order_id).status == OMSOrderStatus.CANCELLED


def test_adopt_renumbers_tws_id_via_perm_id(
    mock_adapter: IBKRExecutionAdapter,
    sample_intent: OrderIntent,
) -> None:
    """M17: unknown tws_id callbacks match adopted order by permId."""
    from app.instruments.models import ResolvedInstrument

    order = OMSOrder(
        internal_order_id="INT-PERM-1",
        intent=sample_intent,
        symbol="EWA",
        side=OrderSide.BUY,
        quantity=1.0,
        ibkr_order_id=100,
        status=OMSOrderStatus.SUBMITTED,
        perm_id=424242,
        resolved=ResolvedInstrument(
            symbol="EWA",
            requested_instrument_type="STK",
            sec_type="STK",
            con_id=123456,
            exchange="SMART",
            currency="USD",
        ),
    )
    mock_adapter.adopt_order(order)
    mock_adapter.on_order_status(
        orderId=200,
        status="Submitted",
        filled=0.0,
        remaining=1.0,
        avgFillPrice=0.0,
        permId=424242,
        parentId=0,
        lastFillPrice=0.0,
        clientId=1,
        whyHeld="",
        mktCapPrice=0.0,
    )
    rebound = mock_adapter._orders_by_tws_id.get(200)
    assert rebound is not None
    assert rebound.internal_order_id == "INT-PERM-1"
    assert rebound.ibkr_order_id == 200

