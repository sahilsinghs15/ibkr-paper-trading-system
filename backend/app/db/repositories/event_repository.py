from datetime import datetime
from typing import Any

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.event import EventLogModel

AUDIT_CATEGORIES: dict[str, list[str]] = {
    "orders": ["basket", "oms", "execution"],
    "signals": ["webhook"],
    "risk": ["rms", "risk_exit", "red_zone", "worker"],
    "reconcile": ["reconcile"],
    "positions": ["position"],
    "system": ["systemd", "system", "session_clock", "watchdog"],
}


class EventRepository:
    """Execution audit events. Does not change order state."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        process: str,
        kind: str,
        detail: dict[str, Any],
        signal_id: int | None = None,
        order_id: int | None = None,
        basket_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> EventLogModel | None:
        values = {
            "process": process,
            "kind": kind,
            "detail": detail,
            "signal_id": signal_id,
            "order_id": order_id,
            "basket_id": basket_id,
            "idempotency_key": idempotency_key,
        }
        if idempotency_key:
            stmt = (
                insert(EventLogModel)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
            )
            await self._session.execute(stmt)
            await self._session.flush()
            result = await self._session.execute(
                select(EventLogModel).where(
                    EventLogModel.idempotency_key == idempotency_key
                )
            )
            return result.scalar_one_or_none()
        row = EventLogModel(**values)
        self._session.add(row)
        await self._session.flush()
        return row

    async def query_events(
        self,
        *,
        category: str | None = None,
        process: str | None = None,
        kind: str | None = None,
        search: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[EventLogModel], int]:
        """Query event_log with category, search, date range, and pagination."""
        stmt = select(EventLogModel)
        count_stmt = select(func.count(EventLogModel.id)).select_from(EventLogModel)

        filters: list[Any] = []
        if category and category.lower() in AUDIT_CATEGORIES:
            filters.append(
                EventLogModel.process.in_(AUDIT_CATEGORIES[category.lower()])
            )
        elif process:
            filters.append(EventLogModel.process == process)

        if kind:
            filters.append(EventLogModel.kind == kind)

        if date_from is not None:
            filters.append(EventLogModel.ts >= date_from)
        if date_to is not None:
            filters.append(EventLogModel.ts <= date_to)

        if search and search.strip():
            raw_s = search.strip()
            pattern = f"%{raw_s}%"
            # JSONB detail field search — astext requires type ignore for Pylance
            search_conds: list[Any] = [
                EventLogModel.kind.ilike(pattern),
                EventLogModel.process.ilike(pattern),
                cast(EventLogModel.detail["symbol"].astext, String).ilike(pattern),  # type: ignore[attr-defined]
                cast(EventLogModel.detail["ibkr_account"].astext, String).ilike(  # type: ignore[attr-defined]
                    pattern
                ),
                cast(EventLogModel.detail["account_id"].astext, String).ilike(pattern),  # type: ignore[attr-defined]
                cast(EventLogModel.detail["message"].astext, String).ilike(pattern),  # type: ignore[attr-defined]
            ]
            if raw_s.isdigit():
                val = int(raw_s)
                search_conds.extend(
                    [
                        EventLogModel.id == val,
                        EventLogModel.signal_id == val,
                        EventLogModel.order_id == val,
                        EventLogModel.basket_id == val,
                    ]
                )
            filters.append(or_(*search_conds))

        if filters:
            stmt = stmt.where(*filters)
            count_stmt = count_stmt.where(*filters)

        total = (await self._session.execute(count_stmt)).scalar() or 0

        stmt = stmt.order_by(EventLogModel.ts.desc(), EventLogModel.id.desc())
        stmt = stmt.limit(limit).offset(offset)

        rows = list((await self._session.execute(stmt)).scalars().all())
        return rows, total
