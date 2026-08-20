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
    load_signals,
    position_leg_payloads,
)
from demo_streaming.stream import PositionStream

logger = logging.getLogger(__name__)


def _signal_fp(sig: dict) -> tuple:
    orders = sig.get("orders") or []
    events = sig.get("events") or []
    orders_fp = tuple(
        (
            o.get("id"),
            o.get("status"),
            o.get("fill_qty"),
            o.get("is_compensation"),
            tuple((e.get("id"), e.get("quantity"), e.get("price")) for e in (o.get("executions") or [])),
        )
        for o in orders
    )
    events_fp = tuple((ev.get("id"), ev.get("kind")) for ev in events)
    return (
        sig.get("status"),
        sig.get("canonical_status"),
        sig.get("is_active_processing"),
        sig.get("reject_reason"),
        orders_fp,
        events_fp,
    )


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
        self._signal_fingerprints: dict[int, tuple] = {}
        self._last_signal_id = 0
        self._baseline_ready = False

    async def restore_baseline(self) -> None:
        """Load current rows so a bridge restart does not re-emit OPEN for existing trades."""
        async with self._session_factory() as session:
            payloads = await self._collect(session)
            sig_res = await load_signals(session, page_size=100, return_dict=True)
            sigs = sig_res.get("signals", []) if isinstance(sig_res, dict) else sig_res
            for s in sigs:
                s_id = int(s.get("id") or 0)
                if s_id > 0:
                    self._signal_fingerprints[s_id] = _signal_fp(s)
                    self._last_signal_id = max(self._last_signal_id, s_id)
        for payload in payloads:
            key = _key(payload)
            self._fingerprints[key] = fingerprint(payload)
            self._status[key] = str(payload.get("status") or "")
            self._last_payload[key] = payload
        self._baseline_ready = True
        logger.info("Demo stream baseline restored: %d legs, last_signal_id=%d", len(self._fingerprints), self._last_signal_id)

    async def poll_once(self) -> list[dict]:
        async with self._session_factory() as session:
            payloads = await self._collect(session)
            sig_res = await load_signals(session, page_size=100, return_dict=True)
            sigs = sig_res.get("signals", []) if isinstance(sig_res, dict) else sig_res
        emitted: list[dict] = []
        for sig in reversed(sigs):
            sig_id = int(sig.get("id") or 0)
            if sig_id <= 0:
                continue
            cur_fp = _signal_fp(sig)
            prev_fp = self._signal_fingerprints.get(sig_id)
            if sig_id > self._last_signal_id or prev_fp != cur_fp:
                record = {"event": "SIGNAL_RECEIVED", **sig}
                await self._stream.xadd(record)
                self._last_signal_id = max(self._last_signal_id, sig_id)
                self._signal_fingerprints[sig_id] = cur_fp
                emitted.append(record)
                logger.info(
                    "Demo stream published signal event: signal_id=%s status=%s pair=%s orders=%d",
                    sig.get("signal_id"),
                    sig.get("status"),
                    sig.get("pair"),
                    len(sig.get("orders") or []),
                )
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
            logger.info(
                "Demo stream published: event=%s account_id=%s trade_id=%s symbol=%s",
                event,
                key[0],
                key[1],
                key[2],
            )
        vanished = [
            key for key in list(self._status) if key not in seen and self._status[key] == "OPEN"
        ]
        closed_from_db = await self._payloads_for_vanished(vanished)
        for key in vanished:
            previous = closed_from_db.get(key) or self._last_payload.get(key, {})
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

    async def _payloads_for_vanished(
        self, keys: list[tuple[int, str, str]]
    ) -> dict[tuple[int, str, str], dict]:
        """Re-read CLOSED rows so realised_pnl is not the stale OPEN snapshot."""
        if not keys:
            return {}
        from demo_streaming.snapshot import load_position_with_account

        out: dict[tuple[int, str, str], dict] = {}
        trades = {(account_id, trade_id) for account_id, trade_id, _symbol in keys}
        now = datetime.now(UTC)
        try:
            async with self._session_factory() as session:
                for account_id, trade_id in trades:
                    pair = await load_position_with_account(session, account_id, trade_id)
                    if pair is None:
                        continue
                    position, account = pair
                    for payload in position_leg_payloads(
                        position, account, [], [], timestamp=now
                    ):
                        out[_key(payload)] = payload
        except Exception:
            logger.exception("Failed to reload vanished position rows for CLOSE events")
        return out

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
