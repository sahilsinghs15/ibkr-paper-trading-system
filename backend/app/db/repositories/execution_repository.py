"""Persist IBKR executions. Identity is execId; commissions are not double-counted."""

from collections.abc import Iterable, Sequence
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.execution import ExecutionModel
from app.oms.models import BrokerExecution


def weighted_average_price(rows: Sequence[ExecutionModel]) -> Decimal | None:
    """Quantity-weighted average execution price. None if no quantity."""
    qty = Decimal(0)
    notional = Decimal(0)
    for row in rows:
        q = Decimal(str(row.quantity))
        p = Decimal(str(row.price))
        if q <= 0:
            continue
        qty += q
        notional += q * p
    if qty <= 0:
        return None
    return notional / qty


def total_commission(rows: Sequence[ExecutionModel]) -> Decimal:
    total = Decimal(0)
    for row in rows:
        if row.commission is None:
            continue
        total += Decimal(str(row.commission))
    return total


def realized_pnl_from_marks(
    *,
    signed_qty: Decimal,
    entry: Decimal,
    exit_mark: Decimal,
) -> Decimal:
    """Long: qty * (exit - entry). Short uses negative qty the same way."""
    return signed_qty * (exit_mark - entry)


class ExecutionRepository:
    """Write-once-per-execId ledger. Commission updates in place, never added twice."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_by_internal_order_id(self, internal_order_id: str) -> list[ExecutionModel]:
        result = await self._session.execute(
            select(ExecutionModel)
            .where(ExecutionModel.internal_order_id == internal_order_id)
            .order_by(ExecutionModel.id)
        )
        return list(result.scalars().all())

    async def list_by_internal_order_ids(
        self, internal_order_ids: Iterable[str]
    ) -> list[ExecutionModel]:
        ids = [i for i in internal_order_ids if i]
        if not ids:
            return []
        result = await self._session.execute(
            select(ExecutionModel)
            .where(ExecutionModel.internal_order_id.in_(ids))
            .order_by(ExecutionModel.id)
        )
        return list(result.scalars().all())

    async def upsert(self, execution: BrokerExecution, *, order_id: int | None, account_id: int | None) -> ExecutionModel:
        values = {
            "exec_id": execution.exec_id,
            "order_id": order_id,
            "account_id": account_id,
            "internal_order_id": execution.internal_order_id,
            "broker_order_id": execution.broker_order_id,
            "symbol": execution.symbol,
            "side": execution.side,
            "quantity": execution.quantity,
            "price": execution.price,
            "commission": execution.commission,
            "commission_currency": execution.commission_currency,
            "realized_pnl": execution.realized_pnl,
            "perm_id": execution.perm_id,
            "executed_at": execution.executed_at,
        }
        update = {
            "order_id": values["order_id"],
            "account_id": values["account_id"],
            "internal_order_id": values["internal_order_id"],
            "broker_order_id": values["broker_order_id"],
            "symbol": values["symbol"],
            "side": values["side"],
            "quantity": values["quantity"],
            "price": values["price"],
            "perm_id": values["perm_id"],
            "executed_at": values["executed_at"],
        }
        if execution.commission is not None:
            update["commission"] = execution.commission
            update["commission_currency"] = execution.commission_currency
        if execution.realized_pnl is not None:
            update["realized_pnl"] = execution.realized_pnl
        stmt = (
            insert(ExecutionModel)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["exec_id"],
                set_=update,
            )
        )
        await self._session.execute(stmt)
        await self._session.flush()
        row = (
            await self._session.execute(
                select(ExecutionModel).where(ExecutionModel.exec_id == execution.exec_id)
            )
        ).scalar_one()
        return row
