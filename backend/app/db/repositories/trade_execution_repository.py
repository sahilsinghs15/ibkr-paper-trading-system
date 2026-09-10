"""Durable Trade Book repository - idempotent upsert on exec_id."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.broker.ibkr.executions import BrokerExecutionLine
from app.db.models.trade_execution import TradeExecutionModel

logger = logging.getLogger(__name__)


def _parse_executed_at(raw: str) -> datetime:
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return datetime.now(UTC)


class TradeExecutionRepository:
    """Persist trade_executions with UNIQUE(exec_id) idempotency."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_one(self, line: BrokerExecutionLine, *, account_id: int, order_id: int | None = None) -> TradeExecutionModel:
        executed_at = _parse_executed_at(line.executed_at)
        # Determine if correction: fetch existing to compare fields for increment
        existing = (await self._session.execute(select(TradeExecutionModel).where(TradeExecutionModel.exec_id == line.exec_id))).scalar_one_or_none()
        is_correction = False
        if existing is not None:
            if (Decimal(str(existing.quantity)) != Decimal(str(line.quantity))
                or Decimal(str(existing.price)) != Decimal(str(line.price))
                or (existing.symbol or "") != (line.symbol or "")
                or str(existing.side) != str(line.side)):
                is_correction = True
        # Build values with Decimal coercion
        values: dict[str, Any] = {
            "exec_id": line.exec_id,
            "account_id": account_id,
            "ibkr_account": line.ibkr_account.strip().upper(),
            "order_id": order_id,
            "symbol": line.symbol.strip().upper() if line.symbol else "",
            "sec_type": line.sec_type or "STK",
            "exchange": line.exchange or "SMART",
            "currency": line.currency or "USD",
            "con_id": int(line.con_id or 0),
            "side": line.side,
            "quantity": Decimal(str(line.quantity)),
            "price": Decimal(str(line.price)),
            "cum_qty": Decimal(str(line.cum_qty)),
            "avg_price": Decimal(str(line.avg_price)),
            "broker_order_id": str(line.broker_order_id) if line.broker_order_id is not None else None,
            "perm_id": line.perm_id,
            "client_id": line.client_id,
            "commission": Decimal(str(line.commission)) if line.commission is not None else None,
            "commission_currency": line.commission_currency,
            "realized_pnl": Decimal(str(line.realized_pnl)) if line.realized_pnl is not None else None,
            "executed_at": executed_at,
            "last_synced_at": datetime.now(UTC),
        }
        # correction_count handling via select then increment to avoid SQL expression complexity
        if existing is not None:
            values["correction_count"] = (existing.correction_count or 0) + (1 if is_correction else 0)
        else:
            values["correction_count"] = 0

        update: dict[str, Any] = {
            "account_id": values["account_id"],
            "ibkr_account": values["ibkr_account"],
            "symbol": values["symbol"],
            "sec_type": values["sec_type"],
            "exchange": values["exchange"],
            "currency": values["currency"],
            "con_id": values["con_id"],
            "side": values["side"],
            "quantity": values["quantity"],
            "price": values["price"],
            "cum_qty": values["cum_qty"],
            "avg_price": values["avg_price"],
            "broker_order_id": values["broker_order_id"],
            "perm_id": values["perm_id"],
            "client_id": values["client_id"],
            "executed_at": values["executed_at"],
            "last_synced_at": values["last_synced_at"],
            "correction_count": values["correction_count"],
        }
        # Only overwrite commission/realized_pnl if new non-None
        if values["commission"] is not None:
            update["commission"] = values["commission"]
            update["commission_currency"] = values["commission_currency"]
        if values["realized_pnl"] is not None:
            update["realized_pnl"] = values["realized_pnl"]
        if order_id is not None:
            update["order_id"] = order_id

        stmt = insert(TradeExecutionModel).values(**values).on_conflict_do_update(index_elements=["exec_id"], set_=update)
        await self._session.execute(stmt)
        await self._session.flush()
        row = (await self._session.execute(select(TradeExecutionModel).where(TradeExecutionModel.exec_id == line.exec_id).execution_options(populate_existing=True))).scalar_one()
        return row

    async def upsert_batch(self, lines: list[BrokerExecutionLine], *, account_id: int, order_map: dict[str, int] | None = None) -> int:
        if not lines:
            return 0
        count = 0
        for line in lines:
            broker_oid = str(line.broker_order_id) if line.broker_order_id is not None else None
            oid = None
            if order_map is not None and broker_oid is not None:
                oid = order_map.get(broker_oid)
            # Also try resolving order_id via DB lookup for trade book detached: optional lazy
            await self.upsert_one(line, account_id=account_id, order_id=oid)
            count += 1
        return count

    async def count_for_account(self, account_id: int) -> int:
        result = await self._session.execute(select(func.count()).select_from(TradeExecutionModel).where(TradeExecutionModel.account_id == account_id))
        return int(result.scalar_one())

    async def list_paginated(
        self,
        *,
        account_id: int,
        page: int = 1,
        page_size: int = 50,
        symbol: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        sort: str = "executed_at",
        direction: str = "desc",
    ) -> tuple[list[TradeExecutionModel], int]:
        base = select(TradeExecutionModel).where(TradeExecutionModel.account_id == account_id)
        if symbol:
            base = base.where(func.upper(TradeExecutionModel.symbol) == symbol.strip().upper())
        if date_from is not None:
            base = base.where(TradeExecutionModel.executed_at >= date_from)
        if date_to is not None:
            base = base.where(TradeExecutionModel.executed_at <= date_to)
        total_res = await self._session.execute(select(func.count()).select_from(base.subquery()))
        total = int(total_res.scalar_one())
        # sort
        col = getattr(TradeExecutionModel, sort, TradeExecutionModel.executed_at)
        if direction.lower() == "asc":
            base = base.order_by(col.asc(), TradeExecutionModel.id.asc())
        else:
            base = base.order_by(col.desc(), TradeExecutionModel.id.desc())
        base = base.offset((page - 1) * page_size).limit(page_size)
        rows = (await self._session.execute(base)).scalars().all()
        return list(rows), total

    async def list_paginated_with_order_status(
        self,
        *,
        account_id: int,
        page: int = 1,
        page_size: int = 50,
        symbol: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        sort: str = "executed_at",
        direction: str = "desc",
    ) -> tuple[list[tuple[TradeExecutionModel, str | None]], int]:
        """Batch join to derive order_status without N+1."""
        from app.db.models.order import OrderModel
        base = select(TradeExecutionModel).where(TradeExecutionModel.account_id == account_id)
        if symbol:
            base = base.where(func.upper(TradeExecutionModel.symbol) == symbol.strip().upper())
        if date_from is not None:
            base = base.where(TradeExecutionModel.executed_at >= date_from)
        if date_to is not None:
            base = base.where(TradeExecutionModel.executed_at <= date_to)
        total_res = await self._session.execute(select(func.count()).select_from(base.subquery()))
        total = int(total_res.scalar_one())
        col = getattr(TradeExecutionModel, sort, TradeExecutionModel.executed_at)
        if direction.lower() == "asc":
            base = base.order_by(col.asc(), TradeExecutionModel.id.asc())
        else:
            base = base.order_by(col.desc(), TradeExecutionModel.id.desc())
        base = base.offset((page - 1) * page_size).limit(page_size)
        rows = (await self._session.execute(base)).scalars().all()
        if not rows:
            return [], total
        # Single batch lookup for order statuses (no N+1)
        oids = [r.order_id for r in rows if r.order_id is not None]
        status_map: dict[int, str] = {}
        if oids:
            orows = (await self._session.execute(select(OrderModel.id, OrderModel.status).where(OrderModel.id.in_(oids)))).all()
            for oid, st in orows:
                status_map[int(oid)] = str(st)
        result: list[tuple[TradeExecutionModel, str | None]] = []
        for r in rows:
            st = status_map.get(int(r.order_id)) if r.order_id is not None else None
            result.append((r, st))
        return result, total

    async def get_last_synced_at(self, account_id: int) -> datetime | None:
        result = await self._session.execute(select(func.max(TradeExecutionModel.last_synced_at)).where(TradeExecutionModel.account_id == account_id))
        return result.scalar_one_or_none()
