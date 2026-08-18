"""Append-only event_log writer with optional idempotency."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.event import EventLogModel


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
                select(EventLogModel).where(EventLogModel.idempotency_key == idempotency_key)
            )
            return result.scalar_one_or_none()
        row = EventLogModel(**values)
        self._session.add(row)
        await self._session.flush()
        return row
