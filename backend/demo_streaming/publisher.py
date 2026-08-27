"""Poll PostgreSQL and publish position diffs to Redis Streams. Read-only."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from demo_streaming.snapshot import (
    classify_event,
    load_baskets,
    load_orders,
    load_position_rows,
    load_signals,
    pnl_fingerprint,
    position_leg_payloads,
    structural_fingerprint,
)
from demo_streaming.stream import PositionStream

logger = logging.getLogger(__name__)

_MIN_POLL_SLEEP_SEC = 0.25


def _signal_fp(sig: dict) -> tuple:
    return (json.dumps(sig, sort_keys=True, default=str),)


class PositionBridge:
    """Observe existing DB rows and XADD changes. Never mutates trading state."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        stream: PositionStream,
        *,
        poll_interval: float = 2.0,
        signal_watch_limit: int = 500,
        pnl_emit_interval: float = 5.0,
    ) -> None:
        self._session_factory = session_factory
        self._stream = stream
        self._poll_interval = poll_interval
        self._signal_watch_limit = signal_watch_limit
        self._pnl_emit_interval = pnl_emit_interval
        self._structural_fingerprints: dict[tuple[int, str, str], tuple] = {}
        self._pnl_fingerprints: dict[tuple[int, str, str], tuple] = {}
        self._status: dict[tuple[int, str, str], str] = {}
        self._last_payload: dict[tuple[int, str, str], dict] = {}
        self._signal_fingerprints: dict[int, tuple] = {}
        self._last_signal_id = 0
        self._last_pnl_emit: dict[tuple[int, str], float] = {}
        self._baseline_ready = False

    async def restore_baseline(self) -> None:
        """Load current rows so a bridge restart does not re-emit OPEN for existing trades."""
        async with self._session_factory() as session:
            payloads = await self._collect(session)
            sig_res = await load_signals(
                session,
                page_size=self._signal_watch_limit,
                return_dict=True,
                for_watch=True,
            )
            sigs = sig_res.get("signals", []) if isinstance(sig_res, dict) else sig_res
            watch_ids = {int(s.get("id") or 0) for s in sigs if int(s.get("id") or 0) > 0}
            self._signal_fingerprints = {
                sid: fp for sid, fp in self._signal_fingerprints.items() if sid in watch_ids
            }
            for s in sigs:
                s_id = int(s.get("id") or 0)
                if s_id > 0:
                    self._signal_fingerprints[s_id] = _signal_fp(s)
                    self._last_signal_id = max(self._last_signal_id, s_id)
        for payload in payloads:
            key = _key(payload)
            self._structural_fingerprints[key] = structural_fingerprint(payload)
            self._pnl_fingerprints[key] = pnl_fingerprint(payload)
            self._status[key] = str(payload.get("status") or "")
            self._last_payload[key] = payload
        self._baseline_ready = True
        logger.info(
            "Demo stream baseline restored: %d legs, last_signal_id=%d",
            len(self._structural_fingerprints),
            self._last_signal_id,
        )

    async def poll_once(self) -> list[dict]:
        async with self._session_factory() as session:
            payloads = await self._collect(session)
            sig_res = await load_signals(
                session,
                page_size=self._signal_watch_limit,
                return_dict=True,
                for_watch=True,
            )
            sigs = sig_res.get("signals", []) if isinstance(sig_res, dict) else sig_res
        watch_ids = {int(s.get("id") or 0) for s in sigs if int(s.get("id") or 0) > 0}
        self._signal_fingerprints = {
            sid: fp for sid, fp in self._signal_fingerprints.items() if sid in watch_ids
        }
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
        pnl_only_by_trade: dict[tuple[int, str], dict] = {}
        for payload in payloads:
            key = _key(payload)
            seen.add(key)
            cur_struct = structural_fingerprint(payload)
            cur_pnl = pnl_fingerprint(payload)
            prev_struct = self._structural_fingerprints.get(key)
            prev_pnl = self._pnl_fingerprints.get(key)
            previous_status = self._status.get(key)
            previous_payload = self._last_payload.get(key)

            if prev_struct == cur_struct and prev_pnl == cur_pnl:
                self._last_payload[key] = payload
                continue

            if not self._baseline_ready:
                self._structural_fingerprints[key] = cur_struct
                self._pnl_fingerprints[key] = cur_pnl
                self._status[key] = str(payload.get("status") or "")
                self._last_payload[key] = payload
                continue

            struct_changed = prev_struct != cur_struct
            pnl_changed = prev_pnl != cur_pnl

            if struct_changed:
                event = classify_event(
                    previous_status=previous_status,
                    current_status=str(payload.get("status") or ""),
                    previous_fill=previous_payload.get("filled_quantity") if previous_payload else None,
                    current_fill=payload.get("filled_quantity"),
                    close_in_progress=bool(payload.get("close_in_progress")),
                )
                record = {"event": event, **payload}
                await self._stream.xadd(record)
                self._structural_fingerprints[key] = cur_struct
                self._pnl_fingerprints[key] = cur_pnl
                self._status[key] = str(payload.get("status") or "")
                self._last_payload[key] = payload
                emitted.append(record)
                self._log_position_event(event, key)
            elif pnl_changed:
                trade_key = (key[0], key[1])
                existing = pnl_only_by_trade.get(trade_key)
                sym = str(payload.get("symbol") or "")
                if existing is None or sym < str(existing.get("symbol") or ""):
                    pnl_only_by_trade[trade_key] = payload

        loop = asyncio.get_running_loop()
        now = loop.time()
        for trade_key, payload in pnl_only_by_trade.items():
            last_emit = self._last_pnl_emit.get(trade_key, 0.0)
            if now - last_emit < self._pnl_emit_interval:
                continue
            key = _key(payload)
            record = {"event": "POSITION_UPDATE", **payload}
            await self._stream.xadd(record)
            self._last_pnl_emit[trade_key] = now
            emitted.append(record)
            self._log_position_event("POSITION_UPDATE", key)
            for leg in payloads:
                if (leg["account_id"], leg["trade_id"]) != trade_key:
                    continue
                leg_key = _key(leg)
                self._structural_fingerprints[leg_key] = structural_fingerprint(leg)
                self._pnl_fingerprints[leg_key] = pnl_fingerprint(leg)
                self._last_payload[leg_key] = leg

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
            self._structural_fingerprints.pop(key, None)
            self._pnl_fingerprints.pop(key, None)
            self._last_payload.pop(key, None)
            emitted.append(record)
            logger.info(
                "Demo stream published: event=POSITION_CLOSED account_id=%s trade_id=%s symbol=%s",
                key[0],
                key[1],
                key[2],
            )
        self._baseline_ready = True
        return emitted

    def _log_position_event(self, event: str, key: tuple[int, str, str]) -> None:
        msg = "Demo stream published: event=%s account_id=%s trade_id=%s symbol=%s"
        args = (event, key[0], key[1], key[2])
        if event == "POSITION_UPDATE":
            logger.debug(msg, *args)
        else:
            logger.info(msg, *args)

    async def run_forever(self) -> None:
        await self.restore_baseline()
        while True:
            started = asyncio.get_running_loop().time()
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Demo position poll failed; will retry")
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(_MIN_POLL_SLEEP_SEC, self._poll_interval - elapsed))

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
        keys = {(position.account_id, position.trade_id) for position, _account in rows}
        baskets = await load_baskets(session, keys)
        orders = await load_orders(session, keys)
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
