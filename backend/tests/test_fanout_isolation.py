"""Fan-out must not cancel sibling accounts when one account raises."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.accounts.context import AccountExecutionContext
from app.models.signal import Signal, SignalType
from app.oms.models import ExecutionResult, OMSOrder, OMSOrderStatus
from app.rms.models import OrderAction, OrderIntent, OrderLeg, OrderSide, RMSOutcome, RMSResult
from app.services.model_blue.parser import MODEL_BLUE_STRATEGY_ID
from app.services.order_manager import OrderManager


def _ctx(account_id: int, ibkr: str) -> AccountExecutionContext:
    return AccountExecutionContext(
        account_id=account_id,
        ibkr_account=ibkr,
        strategy_id=MODEL_BLUE_STRATEGY_ID,
        total_margin=Decimal(100000),
        alloc_pct=Decimal("0.25"),
        committed_notional=Decimal(25000),
        target=Decimal(500),
        stop=Decimal(250),
        time_limit=3600,
        max_open_positions=10,
    )


def _signal() -> Signal:
    return Signal(
        signal_type=SignalType.BUY,
        timestamp=datetime.now(UTC),
        reason="fanout isolation",
        signal_id="T-FANOUT",
        strategy_id=MODEL_BLUE_STRATEGY_ID,
        action="OPEN",
    )


def _execution_result(account_id: int, ibkr: str) -> ExecutionResult:
    intent = OrderIntent(
        signal_id="T-FANOUT",
        strategy_id=MODEL_BLUE_STRATEGY_ID,
        action=OrderAction.OPEN,
        account_id=account_id,
        ibkr_account=ibkr,
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
        timestamp=datetime.now(UTC),
    )
    order = OMSOrder(
        internal_order_id=f"ORD-{account_id}",
        intent=intent,
        symbol="XLE",
        side=OrderSide.BUY,
        quantity=10,
        order_type="MARKET",
        status=OMSOrderStatus.FILLED,
    )
    rms = RMSResult(
        outcome=RMSOutcome.PASS,
        intent=intent,
        original_intent=intent,
        timestamp=datetime.now(UTC),
    )
    return ExecutionResult(order=order, rms_result=rms, success=True, orders=[order])


@pytest.mark.asyncio
async def test_fanout_survives_sibling_runtime_error() -> None:
    """Account B completes even when account A raises after submit."""
    manager = OrderManager(oms=MagicMock(), strategy_id=MODEL_BLUE_STRATEGY_ID)
    ctx_a = _ctx(10, "DU-A")
    ctx_b = _ctx(20, "DU-B")
    submitted: list[int] = []

    async def fake_evaluate(intent, signal, **kwargs):  # noqa: ANN001
        if intent.account_id == 10:
            raise RuntimeError("dictionary changed size during iteration")
        submitted.append(intent.account_id)
        return _execution_result(intent.account_id, intent.ibkr_account or "")

    handler = MagicMock()
    handler.build_intent = AsyncMock(
        side_effect=lambda signal, account: OrderIntent(
            signal_id=signal.signal_id,
            strategy_id=MODEL_BLUE_STRATEGY_ID,
            action=OrderAction.OPEN,
            account_id=account.account_id,
            ibkr_account=account.ibkr_account,
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
            timestamp=datetime.now(UTC),
        )
    )

    with patch.object(manager, "_evaluate_and_submit", fake_evaluate):
        result = await manager._fanout_accounts(
            _signal(), handler, [ctx_a, ctx_b]
        )

    assert result.had_unexpected_error is True
    assert submitted == [20]
    by_id = {o.account_id: o for o in result.outcomes}
    assert by_id[10].error is not None
    assert "dictionary changed size" in by_id[10].error
    assert by_id[20].success is True
