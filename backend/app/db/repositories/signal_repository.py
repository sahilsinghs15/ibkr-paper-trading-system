"""Persistence for TradingView/external signals."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.signal import SignalModel
from app.models.signal import Signal

SIGNAL_STATUS_PROCESSED = "PROCESSED"
SIGNAL_STATUS_NEW = "NEW"


class SignalRepository:
    """Signal inbox access. No sizing logic."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_strategy_signal(
        self, strategy_id: str, signal_id: str
    ) -> SignalModel | None:
        result = await self._session.execute(
            select(SignalModel).where(
                SignalModel.strategy_id == strategy_id,
                SignalModel.signal_id == signal_id,
            )
        )
        return result.scalar_one_or_none()

    async def is_processed(self, strategy_id: str, signal_id: str) -> bool:
        row = await self.get_by_strategy_signal(strategy_id, signal_id)
        return row is not None and row.status == SIGNAL_STATUS_PROCESSED

    async def list_processed_open_keys(self) -> set[tuple[str, str]]:
        result = await self._session.execute(
            select(SignalModel.strategy_id, SignalModel.signal_id).where(
                SignalModel.status == SIGNAL_STATUS_PROCESSED,
                SignalModel.action == "OPEN",
            )
        )
        return {(row[0], row[1]) for row in result.all()}

    async def record_processed(
        self,
        signal: Signal,
        *,
        persist_signal_id: str,
        status: str = SIGNAL_STATUS_PROCESSED,
    ) -> SignalModel:
        """Insert or update a processed signal row."""
        now = datetime.now(UTC)
        pair = ":".join(leg.symbol for leg in signal.legs) if signal.legs else ""
        side = str(signal.direction) if signal.direction is not None else (signal.side or "")
        # signals.ref_price_a/b are existing pair-column schema fields, not generic N-leg storage.
        price_a = signal.legs[0].price if signal.legs else (signal.price or Decimal(0))
        price_b = signal.legs[1].price if len(signal.legs) > 1 else None
        payload: dict[str, Any] = signal.raw_payload if isinstance(signal.raw_payload, dict) else {}
        values = {
            "strategy_id": signal.strategy_id or "",
            "signal_id": persist_signal_id,
            "trade_id": signal.trade_id or persist_signal_id,
            "action": str(signal.action or "").upper(),
            "pair": pair or (signal.symbol or "N/A"),
            "side": side or "N/A",
            "ref_price_a": price_a,
            "ref_price_b": price_b,
            "raw_payload": payload,
            "status": status,
            "processed_at": now if status == SIGNAL_STATUS_PROCESSED else None,
        }
        stmt = (
            insert(SignalModel)
            .values(**values)
            .on_conflict_do_update(
                constraint="uq_signals_strategy_signal",
                set_={
                    "trade_id": values["trade_id"],
                    "status": status,
                    "processed_at": values["processed_at"],
                },
            )
        )
        await self._session.execute(stmt)
        await self._session.flush()
        row = await self.get_by_strategy_signal(signal.strategy_id or "", persist_signal_id)
        if row is None:
            raise RuntimeError(
                f"Failed to persist signal {persist_signal_id} for {signal.strategy_id}."
            )
        return row
