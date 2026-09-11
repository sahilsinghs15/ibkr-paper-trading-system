"""Repositories for Manual Trading persistence (M0.5).

Zero broker calls, zero execution mutation logic. Pure persistence foundation.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.manual_order import (
    ManualAuditEventModel,
    ManualExecutionModel,
    ManualHaltStateModel,
    ManualOrderModel,
    ManualPositionModel,
)

logger = logging.getLogger(__name__)


class DuplicateManualIdempotencyError(Exception):
    """Raised when an order with the same (account_id, idempotency_key) already exists."""


class DuplicateManualExecutionError(Exception):
    """Raised when an execution with the same exec_id already exists."""


class ManualOrderRepository:
    """Persistence for manual orders."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_order(
        self,
        *,
        account_id: int,
        ibkr_account: str,
        idempotency_key: str,
        internal_order_id: str,
        trade_id: str,
        symbol: str,
        side: str,
        quantity: Decimal,
        order_type: str,
        con_id: int = 0,
        sec_type: str = "STK",
        exchange: str = "SMART",
        currency: str = "USD",
        limit_price: Decimal | None = None,
        stop_price: Decimal | None = None,
        tif: str = "DAY",
        outside_rth: bool = False,
        user_id: int | None = None,
        perm_id: int | None = None,
        status: str = "PENDING_SUBMIT",
    ) -> ManualOrderModel:
        row = ManualOrderModel(
            account_id=account_id,
            ibkr_account=ibkr_account.strip().upper(),
            idempotency_key=idempotency_key,
            internal_order_id=internal_order_id,
            trade_id=trade_id,
            perm_id=perm_id,
            symbol=symbol.strip().upper(),
            side=side.strip().upper(),
            quantity=Decimal(str(quantity)),
            order_type=order_type.strip().upper(),
            con_id=con_id,
            sec_type=sec_type.strip().upper(),
            exchange=exchange.strip().upper(),
            currency=currency.strip().upper(),
            limit_price=Decimal(str(limit_price)) if limit_price is not None else None,
            stop_price=Decimal(str(stop_price)) if stop_price is not None else None,
            tif=tif.strip().upper(),
            outside_rth=outside_rth,
            user_id=user_id,
            status=status.strip().upper(),
            source="manual",
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_by_id(
        self, order_id: int, *, account_id: int | None = None, for_update: bool = False
    ) -> ManualOrderModel | None:
        stmt = select(ManualOrderModel).where(ManualOrderModel.id == order_id)
        if account_id is not None:
            stmt = stmt.where(ManualOrderModel.account_id == account_id)
        if for_update:
            stmt = stmt.with_for_update()
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            fills_query = select(
                func.coalesce(func.sum(ManualExecutionModel.quantity), Decimal(0))
            ).where(ManualExecutionModel.manual_order_id == row.id)
            row.filled_quantity = (await self._session.execute(fills_query)).scalar_one()
        return row

    async def get_by_internal_id(self, internal_order_id: str) -> ManualOrderModel | None:
        stmt = select(ManualOrderModel).where(
            ManualOrderModel.internal_order_id == internal_order_id
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            fills_query = select(
                func.coalesce(func.sum(ManualExecutionModel.quantity), Decimal(0))
            ).where(ManualExecutionModel.manual_order_id == row.id)
            row.filled_quantity = (await self._session.execute(fills_query)).scalar_one()
        return row

    async def get_by_idempotency_key(
        self, account_id: int, idempotency_key: str
    ) -> ManualOrderModel | None:
        stmt = select(ManualOrderModel).where(
            ManualOrderModel.account_id == account_id,
            ManualOrderModel.idempotency_key == idempotency_key,
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            fills_query = select(
                func.coalesce(func.sum(ManualExecutionModel.quantity), Decimal(0))
            ).where(ManualExecutionModel.manual_order_id == row.id)
            row.filled_quantity = (await self._session.execute(fills_query)).scalar_one()
        return row

    async def list_orders_for_account(
        self,
        *,
        account_id: int,
        status: str | None = None,
        symbol: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ManualOrderModel], int]:
        base = select(ManualOrderModel).where(ManualOrderModel.account_id == account_id)
        if status:
            base = base.where(ManualOrderModel.status == status.strip().upper())
        if symbol:
            base = base.where(ManualOrderModel.symbol == symbol.strip().upper())

        count_stmt = select(func.count()).select_from(base.subquery())
        total = int((await self._session.execute(count_stmt)).scalar_one())

        stmt = base.order_by(ManualOrderModel.id.desc()).offset(offset).limit(limit)
        rows = list((await self._session.execute(stmt)).scalars().all())
        if rows:
            order_ids = [r.id for r in rows]
            fills_query = (
                select(
                    ManualExecutionModel.manual_order_id,
                    func.coalesce(func.sum(ManualExecutionModel.quantity), Decimal(0)),
                )
                .where(ManualExecutionModel.manual_order_id.in_(order_ids))
                .group_by(ManualExecutionModel.manual_order_id)
            )
            fill_map = dict((await self._session.execute(fills_query)).all())  # type: ignore[arg-type, var-annotated]
            for r in rows:
                r.filled_quantity = fill_map.get(r.id, Decimal(0))
        return rows, total

    async def get_by_broker_order_id(
        self, broker_order_id: str, *, account_id: int | None = None
    ) -> ManualOrderModel | None:
        stmt = (
            select(ManualOrderModel)
            .where(ManualOrderModel.broker_order_id == str(broker_order_id))
            .order_by(ManualOrderModel.id.desc())
        )
        if account_id is not None:
            stmt = stmt.where(ManualOrderModel.account_id == account_id)
        result = await self._session.execute(stmt)
        row = result.scalars().first()
        if row is not None:
            fills_query = select(
                func.coalesce(func.sum(ManualExecutionModel.quantity), Decimal(0))
            ).where(ManualExecutionModel.manual_order_id == row.id)
            row.filled_quantity = (await self._session.execute(fills_query)).scalar_one()
        return row

    async def get_by_perm_id(
        self, perm_id: int, *, account_id: int | None = None
    ) -> ManualOrderModel | None:
        stmt = (
            select(ManualOrderModel)
            .where(ManualOrderModel.perm_id == int(perm_id))
            .order_by(ManualOrderModel.id.desc())
        )
        if account_id is not None:
            stmt = stmt.where(ManualOrderModel.account_id == account_id)
        result = await self._session.execute(stmt)
        row = result.scalars().first()
        if row is not None:
            fills_query = select(
                func.coalesce(func.sum(ManualExecutionModel.quantity), Decimal(0))
            ).where(ManualExecutionModel.manual_order_id == row.id)
            row.filled_quantity = (await self._session.execute(fills_query)).scalar_one()
        return row

    async def get_unresolved_orders(self) -> list[ManualOrderModel]:
        """Fetch manual orders in non-terminal states for recovery correlation."""
        stmt = (
            select(ManualOrderModel)
            .where(
                ManualOrderModel.status.in_(
                    ["PENDING_SUBMIT", "SUBMITTED", "PARTIALLY_FILLED"]
                )
            )
            .order_by(ManualOrderModel.id.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def update_status(
        self,
        order_id: int,
        status: str,
        *,
        broker_order_id: str | None = None,
        perm_id: int | None = None,
        submitted_at: datetime | None = None,
        completed_at: datetime | None = None,
        reject_reason: str | None = None,
    ) -> ManualOrderModel | None:
        row = await self.get_by_id(order_id)
        if row is None:
            return None
        row.status = status.strip().upper()
        if broker_order_id is not None:
            row.broker_order_id = broker_order_id
        if perm_id is not None:
            row.perm_id = perm_id
        if submitted_at is not None:
            row.submitted_at = submitted_at
        if completed_at is not None:
            row.completed_at = completed_at
        if reject_reason is not None:
            row.reject_reason = reject_reason
        await self._session.flush()
        return row


class ManualExecutionRepository:
    """Persistence for manual executions with UNIQUE(exec_id) deduplication."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_execution(
        self,
        *,
        manual_order_id: int,
        exec_id: str,
        quantity: Decimal,
        price: Decimal,
        executed_at: datetime,
        broker_order_id: str | None = None,
        commission: Decimal | None = None,
        commission_currency: str | None = None,
    ) -> ManualExecutionModel:
        """Insert execution fill. Deduplicates on exec_id via unique constraint."""
        row, _ = await self.record_execution_with_dedup(
            manual_order_id=manual_order_id,
            exec_id=exec_id,
            quantity=quantity,
            price=price,
            executed_at=executed_at,
            broker_order_id=broker_order_id,
            commission=commission,
            commission_currency=commission_currency,
        )
        return row

    async def record_execution_with_dedup(
        self,
        *,
        manual_order_id: int,
        exec_id: str,
        quantity: Decimal,
        price: Decimal,
        executed_at: datetime,
        broker_order_id: str | None = None,
        commission: Decimal | None = None,
        commission_currency: str | None = None,
    ) -> tuple[ManualExecutionModel, bool]:
        """Insert execution fill returning (model, is_new).

        If exec_id already exists, returns (existing_row, False) without second mutation.
        """
        values: dict[str, Any] = {
            "manual_order_id": manual_order_id,
            "exec_id": exec_id,
            "broker_order_id": broker_order_id,
            "quantity": Decimal(str(quantity)),
            "price": Decimal(str(price)),
            "commission": Decimal(str(commission)) if commission is not None else None,
            "commission_currency": commission_currency,
            "source": "manual",
            "executed_at": executed_at,
        }
        stmt = (
            insert(ManualExecutionModel)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["exec_id"])
            .returning(ManualExecutionModel)
        )
        result = await self._session.execute(stmt)
        new_row = result.scalar_one_or_none()
        if new_row is not None:
            await self._session.flush()
            return new_row, True

        # Existing row
        existing = (
            await self._session.execute(
                select(ManualExecutionModel).where(ManualExecutionModel.exec_id == exec_id)
            )
        ).scalar_one()
        return existing, False

    async def update_commission(
        self,
        exec_id: str,
        commission: Decimal,
        commission_currency: str | None = None,
    ) -> tuple[ManualExecutionModel | None, bool]:
        """Update commission on manual execution if not already set.

        Returns (execution_row, was_updated).
        was_updated is True only if commission was newly set or changed.
        """
        row = await self.get_by_exec_id(exec_id)
        if row is None:
            return None, False

        # If already set and equal or non-zero, do not re-apply
        if row.commission is not None and row.commission != Decimal(0):
            return row, False

        row.commission = Decimal(str(commission))
        if commission_currency is not None:
            row.commission_currency = commission_currency
        await self._session.flush()
        return row, True

    async def get_by_exec_id(self, exec_id: str) -> ManualExecutionModel | None:
        stmt = select(ManualExecutionModel).where(ManualExecutionModel.exec_id == exec_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_order(self, manual_order_id: int) -> list[ManualExecutionModel]:
        stmt = (
            select(ManualExecutionModel)
            .where(ManualExecutionModel.manual_order_id == manual_order_id)
            .order_by(ManualExecutionModel.id.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_by_order_id(self, manual_order_id: int) -> list[ManualExecutionModel]:
        """Alias for list_for_order."""
        return await self.list_for_order(manual_order_id)


class ManualPositionRepository:
    """Persistence, query, and execution application for manual positions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def apply_execution(
        self,
        *,
        account_id: int,
        trade_id: str,
        symbol: str,
        con_id: int = 0,
        sec_type: str = "CFD",
        currency: str = "USD",
        side: str,
        quantity: Decimal,
        price: Decimal,
        commission: Decimal = Decimal(0),
    ) -> tuple[ManualPositionModel, Decimal]:
        """Apply an execution fill to the manual position ledger.

        Atomic mutation implementing the exact 8 position lifecycle transitions:
        1. Open Long (0 -> +qty)
        2. Add to Long (+qty -> +qty')
        3. Reduce Long (+qty -> +qty'') with realized PnL
        4. Close Long (+qty -> 0) with realized PnL and status='CLOSED'
        5. Open Short (0 -> -qty)
        6. Add to Short (-qty -> -qty')
        7. Reduce Short (-qty -> -qty'') with realized PnL
        8. Flip (Long -> Short or Short -> Long) with realized PnL on closed portion

        Returns: (updated_position, realized_pnl_delta)
        """
        norm_side = side.strip().upper()
        if norm_side in ("BOT", "BUY"):
            d = Decimal(str(quantity))
        elif norm_side in ("SLD", "SELL"):
            d = -Decimal(str(quantity))
        else:
            raise ValueError(f"Invalid execution side: {side}")

        exec_qty = Decimal(str(quantity))
        exec_price = Decimal(str(price))
        comm = Decimal(str(commission)) if commission is not None else Decimal(0)
        now = datetime.now(UTC)

        # Lock position row with with_for_update() if it exists
        stmt = (
            select(ManualPositionModel)
            .where(
                ManualPositionModel.account_id == account_id,
                ManualPositionModel.trade_id == trade_id,
            )
            .with_for_update()
        )
        pos = (await self._session.execute(stmt)).scalar_one_or_none()

        if pos is None:
            # Case 1 & 5: Brand new position
            pos = ManualPositionModel(
                account_id=account_id,
                trade_id=trade_id,
                symbol=symbol.strip().upper(),
                con_id=con_id,
                sec_type=sec_type.strip().upper(),
                currency=currency.strip().upper(),
                signed_qty=d,
                avg_cost=exec_price,
                realized_pnl=-comm,
                status="OPEN",
                source="manual",
                opened_at=now,
            )
            self._session.add(pos)
            await self._session.flush()
            return pos, -comm

        current_qty = pos.signed_qty
        current_avg = pos.avg_cost
        current_pnl = pos.realized_pnl

        # If current_qty is 0 (previously closed position being reopened)
        if current_qty == Decimal(0):
            pos.signed_qty = d
            pos.avg_cost = exec_price
            pos.realized_pnl = current_pnl - comm
            pos.status = "OPEN"
            pos.closed_at = None
            pos.updated_at = now
            await self._session.flush()
            return pos, -comm

        # Check direction: same sign or opposing sign
        same_direction = (current_qty > 0 and d > 0) or (current_qty < 0 and d < 0)

        if same_direction:
            # Case 2 & 6: Adding to position
            new_qty = current_qty + d
            abs_curr = abs(current_qty)
            abs_exec = exec_qty
            abs_new = abs(new_qty)
            new_avg = ((abs_curr * current_avg) + (abs_exec * exec_price)) / abs_new
            pos.signed_qty = new_qty
            pos.avg_cost = new_avg
            pos.realized_pnl = current_pnl - comm
            pos.status = "OPEN"
            pos.updated_at = now
            await self._session.flush()
            return pos, -comm

        # Opposing direction: Reducing, closing, or flipping!
        abs_curr = abs(current_qty)
        close_qty = min(abs_curr, exec_qty)

        # Gross realized P&L on the closed portion
        if current_qty > 0:
            # Closing Long via SELL
            gross_pnl = close_qty * (exec_price - current_avg)
        else:
            # Closing Short via BUY
            gross_pnl = close_qty * (current_avg - exec_price)

        realized_pnl_delta = gross_pnl - comm
        pos.realized_pnl = current_pnl + realized_pnl_delta

        if abs_curr > exec_qty:
            # Case 3 & 7: Partial reduction (same direction remains, avg_cost unchanged)
            pos.signed_qty = current_qty + d
            pos.status = "OPEN"
            pos.updated_at = now
        elif abs_curr == exec_qty:
            # Case 4: Exact close
            pos.signed_qty = Decimal(0)
            pos.status = "CLOSED"
            pos.closed_at = now
            pos.updated_at = now
        else:
            # Case 8: Flip (over-close)
            flip_qty = exec_qty - abs_curr
            new_sign = Decimal(1) if d > 0 else Decimal(-1)
            pos.signed_qty = new_sign * flip_qty
            pos.avg_cost = exec_price  # New position opened at execution price!
            pos.status = "OPEN"
            pos.closed_at = None
            pos.updated_at = now

        await self._session.flush()
        return pos, realized_pnl_delta

    async def create_position(
        self,
        *,
        account_id: int,
        trade_id: str,
        symbol: str,
        con_id: int = 0,
        sec_type: str = "STK",
        currency: str = "USD",
        signed_qty: Decimal = Decimal(0),
        avg_cost: Decimal = Decimal(0),
        realized_pnl: Decimal = Decimal(0),
        status: str = "OPEN",
    ) -> ManualPositionModel:
        row = ManualPositionModel(
            account_id=account_id,
            trade_id=trade_id,
            symbol=symbol.strip().upper(),
            con_id=con_id,
            sec_type=sec_type.strip().upper(),
            currency=currency.strip().upper(),
            signed_qty=Decimal(str(signed_qty)),
            avg_cost=Decimal(str(avg_cost)),
            realized_pnl=Decimal(str(realized_pnl)),
            status=status.strip().upper(),
            source="manual",
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_by_trade_id(
        self, account_id: int, trade_id: str
    ) -> ManualPositionModel | None:
        stmt = select(ManualPositionModel).where(
            ManualPositionModel.account_id == account_id,
            ManualPositionModel.trade_id == trade_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_open_for_account(
        self, account_id: int
    ) -> list[ManualPositionModel]:
        stmt = (
            select(ManualPositionModel)
            .where(
                ManualPositionModel.account_id == account_id,
                ManualPositionModel.status == "OPEN",
            )
            .order_by(ManualPositionModel.opened_at.desc(), ManualPositionModel.id.desc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_all_for_account(
        self, account_id: int
    ) -> list[ManualPositionModel]:
        stmt = (
            select(ManualPositionModel)
            .where(ManualPositionModel.account_id == account_id)
            .order_by(ManualPositionModel.id.desc())
        )
        return list((await self._session.execute(stmt)).scalars().all())


class ManualHaltRepository:
    """Persistence for manual trading halt state per account."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_halt_state(self, account_id: int) -> ManualHaltStateModel | None:
        stmt = select(ManualHaltStateModel).where(
            ManualHaltStateModel.account_id == account_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def set_halt(
        self,
        account_id: int,
        *,
        halted: bool,
        halted_by: str | None = None,
        reason: str | None = None,
    ) -> ManualHaltStateModel:
        now = datetime.now(UTC)
        stmt = (
            insert(ManualHaltStateModel)
            .values(
                account_id=account_id,
                halted=halted,
                halted_by=halted_by,
                halted_at=now if halted else None,
                reason=reason,
            )
            .on_conflict_do_update(
                index_elements=["account_id"],
                set_={
                    "halted": halted,
                    "halted_by": halted_by,
                    "halted_at": now if halted else None,
                    "reason": reason,
                    "updated_at": now,
                },
            )
            .returning(ManualHaltStateModel)
        )
        res = await self._session.execute(stmt)
        await self._session.flush()
        return res.scalar_one()


class ManualAuditRepository:
    """Append-only audit trail for manual trading events."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_event(
        self,
        *,
        account_id: int,
        action: str,
        user_id: int | None = None,
        request_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> ManualAuditEventModel:
        row = ManualAuditEventModel(
            account_id=account_id,
            user_id=user_id,
            action=action.strip().upper(),
            request_id=request_id,
            payload=payload or {},
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def list_events(
        self,
        *,
        account_id: int,
        action: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ManualAuditEventModel], int]:
        base = select(ManualAuditEventModel).where(
            ManualAuditEventModel.account_id == account_id
        )
        if action:
            base = base.where(ManualAuditEventModel.action == action.strip().upper())

        count_stmt = select(func.count()).select_from(base.subquery())
        total = int((await self._session.execute(count_stmt)).scalar_one())

        stmt = (
            base.order_by(ManualAuditEventModel.created_at.desc(), ManualAuditEventModel.id.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        return rows, total
