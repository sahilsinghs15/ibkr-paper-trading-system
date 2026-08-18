"""Persistence for OMS/IBKR order ledger rows."""

import math
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.order import OrderModel
from app.oms.models import OMSOrder

_TERMINAL_ORDER_STATUSES = frozenset({"FILLED", "CANCELLED", "REJECTED", "ERROR"})


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
        ibkr_id = str(order.ibkr_order_id) if order.ibkr_order_id is not None else None
        resolved = getattr(order, "resolved", None)
        if resolved is not None:
            ibkr_contract = resolved.identity_key()
        else:
            itype = "STK"
            if order.intent.legs:
                idx = order.leg_index if order.leg_index is not None else 0
                if 0 <= idx < len(order.intent.legs) and order.intent.legs[idx].instrument_type:
                    itype = order.intent.legs[idx].instrument_type
            ibkr_contract = f"{order.symbol}-{itype}-SMART-USD"
        limit_price = order.limit_price if order.limit_price is not None else Decimal(0)
        qty = Decimal(str(order.quantity))
        filled = Decimal(str(order.filled_quantity or 0))
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
        existing = await self.get_by_internal_id(order.internal_order_id)
        persist_status = order.status.value
        persist_filled = filled
        persist_fill_price = fill_price
        persist_filled_at = filled_at
        if existing is not None and existing.status in _TERMINAL_ORDER_STATUSES:
            persist_status = existing.status
            if existing.fill_qty is not None and existing.fill_qty > persist_filled:
                persist_filled = existing.fill_qty
            if persist_fill_price is None:
                persist_fill_price = existing.fill_price
            if persist_filled_at is None:
                persist_filled_at = existing.filled_at
        values = {
            "signal_id": signal_pk,
            "trade_id": trade_id,
            "internal_order_id": order.internal_order_id,
            "basket_id": basket_id,
            "is_compensation": is_comp,
            "compensation_of_internal_order_id": comp_of,
            "account_id": account_id,
            "strategy_id": strategy_id,
            "leg": leg_label,
            "symbol": order.symbol,
            "ibkr_contract": ibkr_contract,
            "buy_sell": order.side.value,
            "quantity": qty,
            "limit_price": limit_price,
            "status": persist_status,
            "broker_order_id": ibkr_id,
            "fill_price": persist_fill_price,
            "fill_qty": persist_filled,
            "filled_at": persist_filled_at,
        }
        update = {
            "status": values["status"],
            "broker_order_id": values["broker_order_id"],
            "quantity": values["quantity"],
            "limit_price": values["limit_price"],
            "fill_price": values["fill_price"],
            "fill_qty": values["fill_qty"],
            "is_compensation": values["is_compensation"],
        }
        if persist_filled_at is not None:
            update["filled_at"] = persist_filled_at
        if basket_id is not None:
            update["basket_id"] = basket_id
        if comp_of is not None:
            update["compensation_of_internal_order_id"] = comp_of
        stmt = (
            insert(OrderModel)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["internal_order_id"],
                set_=update,
            )
        )
        await self._session.execute(stmt)
        await self._session.flush()
        row = await self.get_by_internal_id(order.internal_order_id)
        if row is None:
            raise RuntimeError(
                f"Failed to persist order {order.internal_order_id}."
            )
        return row
