"""Persistence for IBKR broker position snapshots and reconcile runs."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.broker_position import BrokerPositionModel, PositionReconcileRunModel


class BrokerPositionRepository:
    """Replace-all broker snapshot storage. Does not touch the positions ledger."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace_snapshot(
        self,
        rows: list[dict[str, Any]],
        *,
        as_of: datetime | None = None,
    ) -> int:
        """Delete all broker_positions and insert the latest snapshot."""
        await self._session.execute(delete(BrokerPositionModel))
        snapshot_time = as_of or datetime.now(UTC)
        for row in rows:
            self._session.add(
                BrokerPositionModel(
                    ibkr_account=row["ibkr_account"],
                    con_id=int(row["con_id"]),
                    account_id=row.get("account_id"),
                    symbol=row["symbol"],
                    sec_type=row["sec_type"],
                    currency=row["currency"],
                    exchange=row.get("exchange") or "",
                    signed_qty=Decimal(str(row["signed_qty"])),
                    avg_cost=Decimal(str(row["avg_cost"])),
                    as_of=snapshot_time,
                )
            )
        await self._session.flush()
        return len(rows)

    async def insert_run(
        self,
        *,
        started_at: datetime,
        finished_at: datetime,
        timed_out: bool,
        error: str | None,
        broker_line_count: int,
        match_count: int,
        ghost_count: int,
        orphan_count: int,
        drift_count: int,
        unmapped_account_count: int,
        mismatches: list[dict[str, Any]],
    ) -> PositionReconcileRunModel:
        row = PositionReconcileRunModel(
            started_at=started_at,
            finished_at=finished_at,
            timed_out=timed_out,
            error=error,
            broker_line_count=broker_line_count,
            match_count=match_count,
            ghost_count=ghost_count,
            orphan_count=orphan_count,
            drift_count=drift_count,
            unmapped_account_count=unmapped_account_count,
            mismatches=mismatches,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def list_snapshot(
        self,
        *,
        ibkr_account: str | None = None,
    ) -> list[BrokerPositionModel]:
        """Return persisted broker snapshot rows, optionally filtered by IBKR account."""
        stmt = select(BrokerPositionModel)
        if ibkr_account is not None:
            stmt = stmt.where(BrokerPositionModel.ibkr_account == ibkr_account)
        stmt = stmt.order_by(
            BrokerPositionModel.ibkr_account,
            BrokerPositionModel.symbol,
            BrokerPositionModel.con_id,
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_latest_run(self) -> PositionReconcileRunModel | None:
        """Return the most recent reconcile run row, if any."""
        stmt = (
            select(PositionReconcileRunModel)
            .order_by(PositionReconcileRunModel.id.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_snapshot_line(
        self,
        *,
        ibkr_account: str,
        con_id: int,
    ) -> BrokerPositionModel | None:
        """Return one broker snapshot row by IBKR account and conId."""
        stmt = select(BrokerPositionModel).where(
            BrokerPositionModel.ibkr_account == ibkr_account,
            BrokerPositionModel.con_id == con_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()
