"""Persistence for TradingView/external signals."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import case, literal, not_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.signal import SignalModel
from app.models.signal import Signal, SignalType

SIGNAL_STATUS_PROCESSED = "PROCESSED"
SIGNAL_STATUS_NEW = "NEW"
SIGNAL_STATUS_REJECTED = "REJECTED"


def persist_signal_id_for(signal: Signal) -> str:
    """Stable ``signals.signal_id``: OPEN uses trade_id; CLOSE uses ``{trade_id}:CLOSE``."""
    trade_id = (signal.trade_id or signal.signal_id or "").strip()
    action = str(signal.action or "").upper()
    if action == "CLOSE" and trade_id and not trade_id.endswith(":CLOSE"):
        return f"{trade_id}:CLOSE"
    return trade_id


def original_raw_payload(signal: Signal) -> dict[str, Any]:
    """Return a non-empty JSON object for ``signals.raw_payload``.

    Prefer the webhook capture envelope (``parsed_json`` / ``raw_body``). Never
    return ``{}``. If capture data is missing, reconstruct from the parsed Signal.
    """
    raw = signal.raw_payload
    if isinstance(raw, dict) and raw:
        parsed = raw.get("parsed_json")
        if isinstance(parsed, dict) and parsed:
            return raw
        if "raw_body" in raw or any(k in raw for k in ("strategy", "trade_id", "buckets", "action")):
            return raw
        return raw
    return _reconstruct_payload(signal)


def _reconstruct_payload(signal: Signal) -> dict[str, Any]:
    buckets = []
    for leg in signal.legs:
        buckets.append(
            {
                "underlying": leg.symbol,
                "legs": [
                    {
                        "instrument_type": leg.instrument_type,
                        "side": leg.payload_side,
                        "weight": leg.weight,
                        "price": str(leg.price),
                    }
                ],
            }
        )
    return {
        "strategy": signal.strategy_id,
        "action": str(signal.action or "").upper() or None,
        "trade_id": signal.trade_id or signal.signal_id,
        "direction": signal.direction,
        "market": signal.market,
        "buckets": buckets,
    }


def _audit_values(
    signal: Signal,
    *,
    persist_signal_id: str,
    status: str,
    reject_reason: str | None = None,
) -> dict[str, Any]:
    pair = ":".join(leg.symbol for leg in signal.legs) if signal.legs else ""
    if not pair and signal.trade_id:
        trade_parts = [p.split(":")[-1] for p in signal.trade_id.split("-") if ":" in p]
        if len(trade_parts) >= 2:
            pair = f"{trade_parts[0]}:{trade_parts[1]}"
        elif len(trade_parts) == 1:
            pair = trade_parts[0]
    if signal.direction is not None:
        side = str(signal.direction)
    elif signal.side:
        side = str(signal.side)
    elif signal.legs:
        sides = [str(leg.payload_side or "") for leg in signal.legs if leg.payload_side]
        side = ":".join(sides) if sides else "N/A"
    else:
        side = "N/A"
    price_a = signal.legs[0].price if signal.legs else (signal.price or Decimal(0))
    price_b = signal.legs[1].price if len(signal.legs) > 1 else None
    now = datetime.now(UTC)
    payload = original_raw_payload(signal)
    if not payload:
        raise ValueError("SIGNAL_PAYLOAD_EMPTY: refusing to persist raw_payload={}.")
    return {
        "strategy_id": signal.strategy_id or "",
        "signal_id": persist_signal_id,
        "trade_id": signal.trade_id or persist_signal_id.replace(":CLOSE", ""),
        "action": str(signal.action or "").upper(),
        "pair": pair or (signal.symbol or "N/A"),
        "side": side or "N/A",
        "ref_price_a": price_a,
        "ref_price_b": price_b,
        "raw_payload": payload,
        "status": status,
        "reject_reason": reject_reason,
        "processed_at": now if status == SIGNAL_STATUS_PROCESSED else None,
    }


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

    async def record_inbound(
        self,
        signal: Signal,
        *,
        persist_signal_id: str | None = None,
        status: str = SIGNAL_STATUS_NEW,
        reject_reason: str | None = None,
    ) -> SignalModel:
        """Insert or fill in the audit row as soon as the webhook is parsed."""
        return await self._upsert(
            signal,
            persist_signal_id=persist_signal_id or persist_signal_id_for(signal),
            status=status,
            reject_reason=reject_reason,
        )

    async def record_processed(
        self,
        signal: Signal,
        *,
        persist_signal_id: str,
        status: str = SIGNAL_STATUS_PROCESSED,
    ) -> SignalModel:
        """Insert or update a processed signal row, filling any stub audit columns."""
        return await self._upsert(
            signal,
            persist_signal_id=persist_signal_id,
            status=status,
            reject_reason=None,
        )

    async def record_rejected_payload(
        self,
        payload: dict[str, Any],
        *,
        capture_data: dict[str, Any],
        reason: str,
    ) -> SignalModel:
        """Persist a parse/validation rejection with the original webhook body."""
        strategy_id = str(payload.get("strategy") or payload.get("strategy_id") or "").strip()
        trade_id = str(payload.get("trade_id") or payload.get("signal_id") or "").strip()
        action = str(payload.get("action") or "").strip().upper() or "UNKNOWN"
        persist_id = trade_id or str((capture_data.get("metadata") or {}).get("request_id") or "")
        if not persist_id:
            persist_id = f"REJECTED-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"
        raw = capture_data if isinstance(capture_data, dict) and capture_data else {}
        if not raw.get("parsed_json"):
            raw = {**raw, "parsed_json": payload}
        signal = Signal(
            signal_type=SignalType.HOLD,
            timestamp=datetime.now(UTC),
            reason=reason,
            signal_id=persist_id,
            strategy_id=strategy_id or "unknown",
            action=action,
            trade_id=trade_id or persist_id,
            raw_payload=raw,
        )
        return await self._upsert(
            signal,
            persist_signal_id=persist_id,
            status=SIGNAL_STATUS_REJECTED,
            reject_reason=reason,
        )

    async def _upsert(
        self,
        signal: Signal,
        *,
        persist_signal_id: str,
        status: str,
        reject_reason: str | None,
    ) -> SignalModel:
        if not persist_signal_id:
            raise ValueError("SIGNAL_ID_REQUIRED: cannot persist a signal without signal_id/trade_id.")
        values = _audit_values(
            signal,
            persist_signal_id=persist_signal_id,
            status=status,
            reject_reason=reject_reason,
        )
        stmt = insert(SignalModel).values(**values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_signals_strategy_signal",
            set_={
                "trade_id": values["trade_id"],
                "action": values["action"],
                "pair": case(
                    (stmt.excluded.pair != "", stmt.excluded.pair),
                    else_=SignalModel.pair,
                ),
                "side": case(
                    (stmt.excluded.side != "N/A", stmt.excluded.side),
                    else_=SignalModel.side,
                ),
                "ref_price_a": stmt.excluded.ref_price_a,
                "ref_price_b": stmt.excluded.ref_price_b,
                "raw_payload": case(
                    (
                        SignalModel.raw_payload.op("?")(literal("parsed_json"))
                        & not_(stmt.excluded.raw_payload.op("?")(literal("parsed_json"))),
                        SignalModel.raw_payload,
                    ),
                    else_=stmt.excluded.raw_payload,
                ),
                "reject_reason": stmt.excluded.reject_reason,
                "status": case(
                    (
                        (stmt.excluded.status == SIGNAL_STATUS_PROCESSED)
                        | (stmt.excluded.status == SIGNAL_STATUS_REJECTED),
                        stmt.excluded.status,
                    ),
                    else_=SignalModel.status,
                ),
                "processed_at": case(
                    (SignalModel.processed_at.isnot(None), SignalModel.processed_at),
                    else_=stmt.excluded.processed_at,
                ),
            },
        )
        await self._session.execute(stmt)
        await self._session.flush()
        row = await self.get_by_strategy_signal(signal.strategy_id or "", persist_signal_id)
        if row is None:
            raise RuntimeError(
                f"Failed to persist signal {persist_signal_id} for {signal.strategy_id}."
            )
        await self._session.refresh(row)
        return row
