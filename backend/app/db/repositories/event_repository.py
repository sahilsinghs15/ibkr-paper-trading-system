"""Append-only event_log writer."""

from typing import Any

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
    ) -> EventLogModel:
        row = EventLogModel(
            process=process,
            kind=kind,
            detail=detail,
            signal_id=signal_id,
            order_id=order_id,
        )
        self._session.add(row)
        await self._session.flush()
        return row
