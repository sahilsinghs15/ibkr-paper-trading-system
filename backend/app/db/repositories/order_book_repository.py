"""Order Book DB queries - durable orders table."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.order import OrderModel


class OrderBookRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_paginated(
        self,
        *,
        account_id: int,
        page: int = 1,
        page_size: int = 50,
        status: str | None = None,
        symbol: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        sort: str = "updated_at",
        direction: str = "desc",
    ) -> tuple[list[OrderModel], int]:
        base = select(OrderModel).where(OrderModel.account_id == account_id)
        if status:
            base = base.where(OrderModel.status == status.strip().upper())
        if symbol:
            base = base.where(func.upper(OrderModel.symbol) == symbol.strip().upper())
        if date_from is not None:
            base = base.where(OrderModel.created_at >= date_from)
        if date_to is not None:
            base = base.where(OrderModel.created_at <= date_to)
        total_res = await self._session.execute(select(func.count()).select_from(base.subquery()))
        total = int(total_res.scalar_one())
        col = getattr(OrderModel, sort, OrderModel.updated_at)
        if direction.lower() == "asc":
            base = base.order_by(col.asc(), OrderModel.id.asc())
        else:
            base = base.order_by(col.desc(), OrderModel.id.desc())
        base = base.offset((page - 1) * page_size).limit(page_size)
        rows = (await self._session.execute(base)).scalars().all()
        return list(rows), total

    async def get_by_internal_id(self, internal_order_id: str, *, account_id: int) -> OrderModel | None:
        result = await self._session.execute(select(OrderModel).where(OrderModel.internal_order_id == internal_order_id, OrderModel.account_id == account_id))
        return result.scalar_one_or_none()
