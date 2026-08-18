"""Poll PostgreSQL and publish position diffs to Redis Streams. Read-only."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from demo_streaming.snapshot import (
    classify_event,
    fingerprint,
    load_baskets,
    load_orders,
    load_position_rows,
    position_leg_payloads,
)
from demo_streaming.stream import PositionStream

logger = logging.getLogger(__name__)


class PositionBridge:
    """Observe existing DB rows and XADD changes. Never mutates trading state."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        stream: PositionStream,
        *,
        poll_interval: float = 0.25,
    ) -> None:
        self._session_factory = session_factory
        self._stream = stream
        self._poll_interval = poll_interval
        self._fingerprints: dict[tuple[int, str, str], tuple] = {}
        self._status: dict[tuple[int, str, str], str] = {}
        self._last_payload: dict[tuple[int, str, str], dict] = {}
        self._baseline_ready = False

    async def restore_baseline(self) -> None:
        """Load current rows so a bridge restart does not re-emit OPEN for existing trades."""
        async with self._session_factory() as session:
            payloads = await self._collect(session)
        for payload in payloads:
            key = _key(payload)
            self._fingerprints[key] = fingerprint(payload)
            self._status[key] = str(payload.get("status") or "")
            self._last_payload[key] = payload
        self._baseline_ready = True
        logger.info("Demo stream baseline restored: %d legs", len(self._fingerprints))

    async def poll_once(self) -> list[dict]:
        async with self._session_factory() as session:
            payloads = await self._collect(session)
        emitted: list[dict] = []
        seen: set[tuple[int, str, str]] = set()
        for payload in payloads:
            key = _key(payload)
            seen.add(key)
            current_fp = fingerprint(payload)
            previous_fp = self._fingerprints.get(key)
            previous_status = self._status.get(key)
            if previous_fp == current_fp:
                self._last_payload[key] = payload
                continue
            if not self._baseline_ready:
                self._fingerprints[key] = current_fp
                self._status[key] = str(payload.get("status") or "")
                self._last_payload[key] = payload
                continue
            event = classify_event(
                previous_status=previous_status,
                current_status=str(payload.get("status") or ""),
                previous_fill=previous_fp[1] if previous_fp else None,
                current_fill=payload.get("filled_quantity"),
                close_in_progress=bool(payload.get("close_in_progress")),
            )
            record = {"event": event, **payload}
            await self._stream.xadd(record)
            self._fingerprints[key] = current_fp
            self._status[key] = str(payload.get("status") or "")
            self._last_payload[key] = payload
            emitted.append(record)
        closed = [key for key in list(self._status) if key not in seen and self._status[key] == "OPEN"]
        for key in closed:
            previous = self._last_payload.get(key, {})
            record = {
                **previous,
                "event": "POSITION_CLOSED",
                "timestamp": datetime.now(UTC).isoformat(),
                "status": "CLOSED",
                "position_state": "CLOSED",
            }
            record["account_id"] = key[0]
            record["trade_id"] = key[1]
            record["symbol"] = key[2]
            await self._stream.xadd(record)
            self._status[key] = "CLOSED"
            self._fingerprints.pop(key, None)
            self._last_payload.pop(key, None)
            emitted.append(record)
        self._baseline_ready = True
        return emitted

    async def run_forever(self) -> None:
        await self.restore_baseline()
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Demo position poll failed; will retry")
            await asyncio.sleep(self._poll_interval)

    async def _collect(self, session: AsyncSession) -> list[dict]:
        now = datetime.now(UTC)
        rows = await load_position_rows(session)
        baskets = await load_baskets(session)
        orders = await load_orders(session)
        payloads: list[dict] = []
        for position, account in rows:
            key = (position.account_id, position.trade_id)
            payloads.extend(
                position_leg_payloads(
                    position,
                    account,
                    baskets.get(key, []),
                    orders.get(key, []),
                    timestamp=now,
                )
            )
        return payloads


def _key(payload: dict) -> tuple[int, str, str]:
    return (int(payload["account_id"]), str(payload["trade_id"]), str(payload["symbol"]))
