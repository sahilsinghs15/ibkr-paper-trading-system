"""Persistence for OMS/IBKR order ledger rows."""

import math
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.order import OrderModel
from app.oms.models import OMSOrder


class OrderRepository:
    """Records OMS orders. Does not submit to the broker."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_internal_id(self, internal_order_id: str) -> OrderModel | None:
        result = await self._session.execute(
            select(OrderModel).where(OrderModel.internal_order_id == internal_order_id)
        )
        return result.scalar_one_or_none()

    async def list_by_trade_id(self, trade_id: str) -> list[OrderModel]:
        result = await self._session.execute(
            select(OrderModel).where(OrderModel.trade_id == trade_id).order_by(OrderModel.id)
        )
        return list(result.scalars().all())

    async def record_oms_order(
        self,
        order: OMSOrder,
        *,
        signal_pk: int,
        account_id: int,
        trade_id: str,
        strategy_id: str,
        leg_label: str,
    ) -> OrderModel:
        existing = await self.get_by_internal_id(order.internal_order_id)
        ibkr_id = str(order.ibkr_order_id) if order.ibkr_order_id is not None else None
        limit_price = order.limit_price if order.limit_price is not None else Decimal(0)
        qty = Decimal(str(order.quantity))
        filled = Decimal(str(order.filled_quantity)) if order.filled_quantity else None
        fill_price = order.average_fill_price or order.last_fill_price
        if fill_price is not None:
            try:
                if not math.isfinite(float(fill_price)) or abs(float(fill_price)) >= 1e12:
                    fill_price = None
            except (TypeError, ValueError):
                fill_price = None
        is_comp = bool(getattr(order, "is_compensation", False))
        comp_of = getattr(order, "compensation_of_internal_order_id", None)
        basket_id = getattr(order, "basket_id", None)
        filled_at = None
        if order.timestamps.execution_received_at is not None:
            filled_at = order.timestamps.execution_received_at
        if existing is None:
            row = OrderModel(
                signal_id=signal_pk,
                trade_id=trade_id,
                internal_order_id=order.internal_order_id,
                basket_id=basket_id,
                is_compensation=is_comp,
                compensation_of_internal_order_id=comp_of,
                account_id=account_id,
                strategy_id=strategy_id,
                leg=leg_label,
                symbol=order.symbol,
                ibkr_contract=f"{order.symbol}-STK-SMART-USD",
                buy_sell=order.side.value,
                quantity=qty,
                limit_price=limit_price,
                status=order.status.value,
                broker_order_id=ibkr_id,
                fill_price=fill_price,
                fill_qty=filled,
                filled_at=filled_at,
            )
            self._session.add(row)
            await self._session.flush()
            return row

        existing.status = order.status.value
        existing.broker_order_id = ibkr_id
        existing.quantity = qty
        existing.limit_price = limit_price
        existing.fill_price = fill_price
        existing.fill_qty = filled
        existing.filled_at = filled_at or existing.filled_at
        if basket_id is not None:
            existing.basket_id = basket_id
        existing.is_compensation = is_comp
        if comp_of is not None:
            existing.compensation_of_internal_order_id = comp_of
        await self._session.flush()
        return existing
