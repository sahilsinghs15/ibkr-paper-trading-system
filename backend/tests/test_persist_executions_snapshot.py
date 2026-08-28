"""Regression: _persist_child must snapshot order.executions before iterating."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.oms.basket import Basket, BasketState
from app.oms.coordinator import BasketCoordinator
from app.oms.models import BrokerExecution, OMSOrder, OMSOrderStatus
from app.rms.models import OrderAction, OrderIntent, OrderLeg, OrderSide


def _order_with_execution() -> OMSOrder:
    intent = OrderIntent(
        signal_id="T-SNAP",
        strategy_id="synthetic_n_leg",
        action=OrderAction.OPEN,
        account_id=1,
        ibkr_account="DUTEST",
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
        internal_order_id="int-snap-1",
        intent=intent,
        symbol="XLE",
        side=OrderSide.BUY,
        quantity=10,
        order_type="MARKET",
        status=OMSOrderStatus.FILLED,
    )
    order.executions["EX-1"] = BrokerExecution(
        exec_id="EX-1",
        internal_order_id=order.internal_order_id,
        symbol="XLE",
        side="BOT",
        quantity=Decimal("10"),
        price=Decimal("50"),
    )
    return order


@pytest.mark.asyncio
async def test_persist_child_survives_executions_dict_mutation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """execDetails can append to order.executions while upsert awaits."""
    coord = BasketCoordinator(MagicMock(), session_factory=session_factory)
    order = _order_with_execution()
    basket = Basket(
        id=1,
        account_id=1,
        trade_id="T-SNAP",
        strategy_id="synthetic_n_leg",
        action="OPEN",
        intended_leg_count=1,
        state=BasketState.EXECUTING,
    )
    upserted_ids: list[str] = []

    async def mutating_upsert(_self, execution, **kwargs):  # noqa: ANN001
        upserted_ids.append(execution.exec_id)
        order.executions["EX-2"] = BrokerExecution(
            exec_id="EX-2",
            internal_order_id=order.internal_order_id,
            symbol="XLE",
            side="BOT",
            quantity=Decimal("5"),
            price=Decimal("50"),
        )

    with (
        patch(
            "app.oms.coordinator.OrderRepository.record_oms_order",
            new_callable=AsyncMock,
        ),
        patch(
            "app.oms.coordinator.OrderRepository.get_by_internal_id",
            new_callable=AsyncMock,
            return_value=MagicMock(id=99),
        ),
        patch(
            "app.oms.coordinator.ExecutionRepository.upsert",
            mutating_upsert,
        ),
    ):
        await coord._persist_child(
            order,
            order.intent,
            signal_pk=1,
            basket=basket,
        )

    assert upserted_ids == ["EX-1"]
