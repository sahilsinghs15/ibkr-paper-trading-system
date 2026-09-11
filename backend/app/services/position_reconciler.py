"""Periodic IBKR broker-vs-ledger position reconciliation (snapshot + log only)."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

try:
    from datetime import UTC  # Python 3.11+
except ImportError:  # pragma: no cover
    UTC = timezone.utc  # type: ignore[assignment]

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.broker.ibkr.positions import BrokerPositionLine
from app.core.identifiers import normalize_account
from app.db.models.account import AccountModel
from app.db.models.basket import BasketModel
from app.db.models.instrument import InstrumentModel
from app.db.models.manual_order import ManualOrderModel, ManualPositionModel
from app.db.models.order import OrderModel
from app.db.models.position import PositionModel
from app.db.models.signal import JOB_STATUS_PROCESSING, SignalJobModel
from app.db.repositories.broker_position_repository import BrokerPositionRepository
from app.db.repositories.event_repository import EventRepository
from app.services.notification_canonical import send_canonical_telegram

logger = logging.getLogger(__name__)

RECONCILE_INTERVAL_SEC = 30.0
POSITIONS_REQUEST_TIMEOUT_SEC = 15.0
QTY_EPSILON = 1e-6

MISMATCH_MATCH = "MATCH"
MISMATCH_LEDGER_GHOST = "LEDGER_GHOST"
MISMATCH_BROKER_ORPHAN = "BROKER_ORPHAN"
MISMATCH_QTY_DRIFT = "QTY_DRIFT"
MISMATCH_UNMAPPED_ACCOUNT = "UNMAPPED_ACCOUNT"

# Rogue tracking key: (ibkr_account_or_id, symbol, sec_type, kind)
RogueKey = tuple[str, str, str, str]


@dataclass(frozen=True)
class LedgerNetLine:
    account_id: int
    symbol: str
    sec_type: str
    signed_qty: Decimal
    con_ids: frozenset[int]
    engine_qty: Decimal = Decimal(0)
    manual_qty: Decimal = Decimal(0)


@dataclass(frozen=True)
class ReconcileDiff:
    kind: str
    ibkr_account: str | None
    account_id: int | None
    symbol: str
    sec_type: str
    con_id: int | None
    broker_qty: float | None
    ledger_qty: float | None
    in_flight: bool
    engine_qty: float | None = None
    manual_qty: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ibkr_account": self.ibkr_account,
            "account_id": self.account_id,
            "symbol": self.symbol,
            "sec_type": self.sec_type,
            "con_id": self.con_id,
            "broker_qty": self.broker_qty,
            "ledger_qty": self.ledger_qty,
            "in_flight": self.in_flight,
            "engine_qty": self.engine_qty,
            "manual_qty": self.manual_qty,
        }


def _qty_close(a: float | Decimal | None, b: float | Decimal | None) -> bool:
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= QTY_EPSILON


def _norm_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def _norm_sec_type(sec_type: str) -> str:
    return sec_type.strip().upper()


def _con_ids_for_symbol(
    symbol_to_conids: dict[tuple[str, str], set[int]],
    symbol: str,
    sec_type: str,
) -> set[int]:
    """Instrument catalog lookup with STK/CFD fallback for Model Blue legs."""
    con_ids = symbol_to_conids.get((symbol, sec_type), set())
    if con_ids:
        return con_ids
    if sec_type == "STK":
        return symbol_to_conids.get((symbol, "CFD"), set())
    if sec_type == "CFD":
        return symbol_to_conids.get((symbol, "STK"), set())
    return set()


def _resolve_ibkr_account(
    account_id: int | None,
    broker_group: list[Any],
    account_to_ibkr: dict[int, str],
) -> str | None:
    if broker_group:
        return broker_group[0].ibkr_account
    if account_id is not None:
        return account_to_ibkr.get(account_id)
    return None


def build_order_con_id_map(
    order_rows: list[Any],
) -> dict[tuple[int, str, str], int]:
    """Latest con_id per (account_id, symbol, sec_type) from persisted order contracts."""
    from app.services.critical_recovery import parse_ibkr_contract

    result: dict[tuple[int, str, str], int] = {}
    for order in order_rows:
        sym, sec, _, _, con_id = parse_ibkr_contract(order.ibkr_contract)
        if con_id is None:
            continue
        key = (order.account_id, _norm_symbol(sym), _norm_sec_type(sec))
        result[key] = con_id
    return result


def _resolve_diff_con_id(
    *,
    account_id: int,
    symbol: str,
    sec_type: str,
    broker_group: list[Any],
    ledger: LedgerNetLine | None,
    order_con_id_map: dict[tuple[int, str, str], int],
) -> int | None:
    if broker_group:
        return int(broker_group[0].con_id)
    if ledger is not None and ledger.con_ids:
        return next(iter(ledger.con_ids))
    con_id = order_con_id_map.get((account_id, symbol, sec_type))
    if con_id is not None:
        return con_id
    alt_sec = "CFD" if sec_type == "STK" else "STK" if sec_type == "CFD" else None
    if alt_sec is not None:
        return order_con_id_map.get((account_id, symbol, alt_sec))
    return None


def build_ledger_net_lines(
    open_rows: list[PositionModel],
    instruments: list[InstrumentModel],
    manual_open_rows: list[ManualPositionModel] | None = None,
) -> list[LedgerNetLine]:
    """Net OPEN ledger qty per (account_id, symbol) from pair legs and manual positions."""
    symbol_to_conids: dict[tuple[str, str], set[int]] = defaultdict(set)
    for inst in instruments:
        key = (_norm_symbol(inst.symbol), _norm_sec_type(inst.sec_type))
        symbol_to_conids[key].add(inst.trade_conid)

    engine_nets: dict[tuple[int, str, str], Decimal] = defaultdict(lambda: Decimal(0))
    manual_nets: dict[tuple[int, str, str], Decimal] = defaultdict(lambda: Decimal(0))

    for row in open_rows:
        legs = [
            (
                row.leg_a_symbol,
                getattr(row, "leg_a_instrument_type", "STK"),
                row.leg_a_signed_qty,
            ),
            (
                row.leg_b_symbol,
                getattr(row, "leg_b_instrument_type", None),
                row.leg_b_signed_qty,
            ),
        ]
        for symbol, inst_type, signed_qty in legs:
            if not symbol or signed_qty is None:
                continue
            sec_type = _norm_sec_type(inst_type or "STK")
            net_key = (row.account_id, _norm_symbol(symbol), sec_type)
            engine_nets[net_key] += Decimal(str(signed_qty))

    if manual_open_rows:
        for mpos in manual_open_rows:
            if not mpos.symbol or mpos.signed_qty is None:
                continue
            sec_type = _norm_sec_type(mpos.sec_type or "CFD")
            net_key = (mpos.account_id, _norm_symbol(mpos.symbol), sec_type)
            manual_nets[net_key] += Decimal(str(mpos.signed_qty))
            if mpos.con_id and int(mpos.con_id) > 0:
                symbol_to_conids[(_norm_symbol(mpos.symbol), sec_type)].add(int(mpos.con_id))

    all_keys = set(engine_nets.keys()) | set(manual_nets.keys())
    result: list[LedgerNetLine] = []
    for (account_id, symbol, sec_type) in sorted(all_keys):
        eq = engine_nets.get((account_id, symbol, sec_type), Decimal(0))
        mq = manual_nets.get((account_id, symbol, sec_type), Decimal(0))
        total_qty = eq + mq
        if abs(float(total_qty)) <= QTY_EPSILON:
            continue
        con_ids = _con_ids_for_symbol(symbol_to_conids, symbol, sec_type)
        result.append(
            LedgerNetLine(
                account_id=account_id,
                symbol=symbol,
                sec_type=sec_type,
                signed_qty=total_qty,
                con_ids=frozenset(con_ids),
                engine_qty=eq,
                manual_qty=mq,
            )
        )
    return result


def ledger_net_qty_for_symbol(
    open_rows: list[PositionModel],
    instruments: list[InstrumentModel],
    *,
    account_id: int,
    symbol: str,
    sec_type: str,
    manual_open_rows: list[ManualPositionModel] | None = None,
) -> float | None:
    """Signed ledger net for one account/symbol, or None when no OPEN net."""
    norm_symbol = _norm_symbol(symbol)
    norm_sec_type = _norm_sec_type(sec_type)
    for line in build_ledger_net_lines(open_rows, instruments, manual_open_rows):
        if (
            line.account_id == account_id
            and line.symbol == norm_symbol
            and line.sec_type == norm_sec_type
        ):
            return float(line.signed_qty)
    return None


def classify_reconcile_diffs(
    *,
    broker_lines: list[BrokerPositionLine],
    ledger_lines: list[LedgerNetLine],
    ibkr_to_account: dict[str, int],
    account_to_ibkr: dict[int, str] | None = None,
    order_con_id_map: dict[tuple[int, str, str], int] | None = None,
    timed_out: bool,
    in_flight_accounts: set[int],
) -> list[ReconcileDiff]:
    """Compare broker snapshot to OPEN ledger nets. Read-only classification."""
    if account_to_ibkr is None:
        account_to_ibkr = {v: k for k, v in ibkr_to_account.items()}
    if order_con_id_map is None:
        order_con_id_map = {}

    broker_by_key: dict[tuple[int, str, str], list[BrokerPositionLine]] = defaultdict(
        list
    )
    unmapped: list[BrokerPositionLine] = []

    for line in broker_lines:
        account_id = ibkr_to_account.get(normalize_account(line.ibkr_account))
        if account_id is None:
            unmapped.append(line)
            continue
        key = (account_id, _norm_symbol(line.symbol), _norm_sec_type(line.sec_type))
        broker_by_key[key].append(line)

    ledger_by_key: dict[tuple[int, str, str], LedgerNetLine] = {
        (line.account_id, line.symbol, line.sec_type): line for line in ledger_lines
    }

    diffs: list[ReconcileDiff] = []
    for line in unmapped:
        diffs.append(
            ReconcileDiff(
                kind=MISMATCH_UNMAPPED_ACCOUNT,
                ibkr_account=line.ibkr_account,
                account_id=None,
                symbol=_norm_symbol(line.symbol),
                sec_type=_norm_sec_type(line.sec_type),
                con_id=line.con_id,
                broker_qty=line.quantity,
                ledger_qty=None,
                in_flight=False,
            )
        )

    all_keys = set(broker_by_key.keys()) | set(ledger_by_key.keys())
    for key in sorted(all_keys):
        account_id, symbol, sec_type = key
        broker_group = broker_by_key.get(key, [])
        ledger = ledger_by_key.get(key)
        broker_qty = (
            sum(line.quantity for line in broker_group) if broker_group else None
        )
        ledger_qty = float(ledger.signed_qty) if ledger is not None else None
        engine_qty = float(ledger.engine_qty) if ledger is not None else None
        manual_qty = float(ledger.manual_qty) if ledger is not None else None
        in_flight = account_id in in_flight_accounts
        ibkr_account = _resolve_ibkr_account(account_id, broker_group, account_to_ibkr)
        con_id = _resolve_diff_con_id(
            account_id=account_id,
            symbol=symbol,
            sec_type=sec_type,
            broker_group=broker_group,
            ledger=ledger,
            order_con_id_map=order_con_id_map,
        )

        if broker_qty is not None and ledger_qty is not None:
            if _qty_close(broker_qty, ledger_qty):
                diffs.append(
                    ReconcileDiff(
                        kind=MISMATCH_MATCH,
                        ibkr_account=ibkr_account,
                        account_id=account_id,
                        symbol=symbol,
                        sec_type=sec_type,
                        con_id=con_id,
                        broker_qty=broker_qty,
                        ledger_qty=ledger_qty,
                        in_flight=in_flight,
                        engine_qty=engine_qty,
                        manual_qty=manual_qty,
                    )
                )
            else:
                diffs.append(
                    ReconcileDiff(
                        kind=MISMATCH_QTY_DRIFT,
                        ibkr_account=ibkr_account,
                        account_id=account_id,
                        symbol=symbol,
                        sec_type=sec_type,
                        con_id=con_id,
                        broker_qty=broker_qty,
                        ledger_qty=ledger_qty,
                        in_flight=in_flight,
                        engine_qty=engine_qty,
                        manual_qty=manual_qty,
                    )
                )
        elif broker_qty is not None and ledger_qty is None:
            diffs.append(
                ReconcileDiff(
                    kind=MISMATCH_BROKER_ORPHAN,
                    ibkr_account=ibkr_account,
                    account_id=account_id,
                    symbol=symbol,
                    sec_type=sec_type,
                    con_id=con_id,
                    broker_qty=broker_qty,
                    ledger_qty=None,
                    in_flight=in_flight,
                    engine_qty=None,
                    manual_qty=None,
                )
            )
        elif broker_qty is None and ledger_qty is not None:
            if timed_out:
                continue
            diffs.append(
                ReconcileDiff(
                    kind=MISMATCH_LEDGER_GHOST,
                    ibkr_account=ibkr_account,
                    account_id=account_id,
                    symbol=symbol,
                    sec_type=sec_type,
                    con_id=con_id,
                    broker_qty=None,
                    ledger_qty=ledger_qty,
                    in_flight=in_flight,
                    engine_qty=engine_qty,
                    manual_qty=manual_qty,
                )
            )

    return diffs


class PositionReconciler:
    """Background loop: fetch IBKR positions, persist snapshot, log ledger diffs."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: Any,
        *,
        interval_sec: float = RECONCILE_INTERVAL_SEC,
        request_timeout_sec: float = POSITIONS_REQUEST_TIMEOUT_SEC,
        after_sweep: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._client = client
        self._interval_sec = interval_sec
        self._request_timeout_sec = request_timeout_sec
        self._after_sweep = after_sweep
        self._task: asyncio.Task | None = None
        self._running = False
        self._sweep_lock = asyncio.Lock()
        self._active_rogue_keys: dict[RogueKey, dict[str, Any]] | None = None

    async def _ensure_active_rogue_keys(self, repo: BrokerPositionRepository) -> None:
        if self._active_rogue_keys is not None:
            return
        self._active_rogue_keys = {}
        try:
            latest_run = await repo.get_latest_run()
            if latest_run and latest_run.mismatches:
                # mismatches is list[dict] persisted from previous run
                for m in latest_run.mismatches:  # type: ignore[attr-defined]
                    if not isinstance(m, dict):
                        continue
                    kind = m.get("kind")
                    if kind in (
                        MISMATCH_QTY_DRIFT,
                        MISMATCH_BROKER_ORPHAN,
                        MISMATCH_LEDGER_GHOST,
                    ):
                        acc = str(m.get("ibkr_account") or m.get("account_id") or "")
                        sym = str(m.get("symbol") or "")
                        sec = str(m.get("sec_type") or "")
                        key = (acc, sym, sec, str(kind))
                        self._active_rogue_keys[key] = {
                            "rogue_type": kind,
                            "symbol": sym,
                            "sec_type": sec,
                            "con_id": m.get("con_id"),
                            "account_id": m.get("account_id"),
                            "ibkr_account": m.get("ibkr_account"),
                            "broker_qty": m.get("broker_qty"),
                            "ledger_qty": m.get("ledger_qty"),
                            "in_flight": m.get("in_flight", False),
                            "run_id": latest_run.id,
                        }
        except Exception:
            logger.exception(
                "Failed loading previous reconcile mismatches for active rogue tracking"
            )

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="position-reconciler")
        logger.info(
            "PositionReconciler started (interval=%.1fs timeout=%.1fs)",
            self._interval_sec,
            self._request_timeout_sec,
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("PositionReconciler stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Position reconcile sweep failed")
            try:
                await asyncio.sleep(self._interval_sec)
            except asyncio.CancelledError:
                break

    async def run_once(self) -> None:
        """Execute one reconcile sweep (skips if already running or TWS disconnected)."""
        if self._sweep_lock.locked():
            logger.debug(
                "Position reconcile sweep skipped: previous sweep still running"
            )
            return
        async with self._sweep_lock:
            started_at = datetime.now(UTC)
            if not getattr(self._client, "is_connected", lambda: False)():
                logger.debug("Position reconcile sweep skipped: TWS not connected")
                return

            timed_out = False
            error: str | None = None
            broker_lines: list[BrokerPositionLine] = []
            try:
                request_async: Any = getattr(
                    self._client, "request_positions_async", None
                )
                if callable(request_async):
                    coro: Any = request_async(timeout=self._request_timeout_sec)
                    broker_lines, timed_out = await coro
                else:
                    error = "TWSClient.request_positions_async unavailable"
            except Exception as exc:
                logger.exception("Failed to fetch IBKR positions")
                error = str(exc)

            await self._persist_and_diff(
                broker_lines=broker_lines,
                started_at=started_at,
                timed_out=timed_out,
                error=error,
            )
            sweep_cb: Any = self._after_sweep
            if callable(sweep_cb):
                try:
                    sweep_coro: Any = sweep_cb()
                    await sweep_coro
                except Exception:
                    logger.exception("Position reconcile after_sweep failed")

    async def _persist_and_diff(
        self,
        *,
        broker_lines: list[BrokerPositionLine],
        started_at: datetime,
        timed_out: bool,
        error: str | None,
    ) -> None:
        finished_at = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            accounts = list(
                (await session.execute(select(AccountModel))).scalars().all()
            )
            ibkr_to_account = {
                normalize_account(acc.ibkr_account): acc.id for acc in accounts
            }
            account_to_ibkr = {acc.id: acc.ibkr_account for acc in accounts}

            snapshot_rows = [
                {
                    "ibkr_account": line.ibkr_account,
                    "con_id": line.con_id,
                    "account_id": ibkr_to_account.get(
                        normalize_account(line.ibkr_account)
                    ),
                    "symbol": _norm_symbol(line.symbol),
                    "sec_type": _norm_sec_type(line.sec_type),
                    "currency": line.currency,
                    "exchange": line.exchange,
                    "signed_qty": line.quantity,
                    "avg_cost": line.avg_cost,
                }
                for line in broker_lines
            ]
            repo = BrokerPositionRepository(session)
            if error is None:
                await repo.replace_snapshot(snapshot_rows, as_of=finished_at)

            open_rows = list(
                (
                    await session.execute(
                        select(PositionModel).where(PositionModel.risk_state == "OPEN")
                    )
                )
                .scalars()
                .all()
            )
            manual_open_rows = list(
                (
                    await session.execute(
                        select(ManualPositionModel).where(
                            ManualPositionModel.status == "OPEN"
                        )
                    )
                )
                .scalars()
                .all()
            )
            instruments = list(
                (await session.execute(select(InstrumentModel))).scalars().all()
            )
            in_flight_accounts = await fetch_in_flight_accounts(session)

            account_ids = {row.account_id for row in open_rows} | {
                row.account_id for row in manual_open_rows
            }
            order_rows: list[OrderModel] = []
            if account_ids:
                order_rows = list(
                    (
                        await session.execute(
                            select(OrderModel)
                            .where(OrderModel.account_id.in_(account_ids))
                            .order_by(OrderModel.id)
                        )
                    )
                    .scalars()
                    .all()
                )
            order_con_id_map = build_order_con_id_map(order_rows)

            ledger_lines = build_ledger_net_lines(
                open_rows, instruments, manual_open_rows
            )
            diffs = classify_reconcile_diffs(
                broker_lines=broker_lines,
                ledger_lines=ledger_lines,
                ibkr_to_account=ibkr_to_account,
                account_to_ibkr=account_to_ibkr,
                order_con_id_map=order_con_id_map,
                timed_out=timed_out,
                in_flight_accounts=in_flight_accounts,
            )

            match_count = sum(1 for d in diffs if d.kind == MISMATCH_MATCH)
            ghost_count = sum(1 for d in diffs if d.kind == MISMATCH_LEDGER_GHOST)
            orphan_count = sum(1 for d in diffs if d.kind == MISMATCH_BROKER_ORPHAN)
            drift_count = sum(1 for d in diffs if d.kind == MISMATCH_QTY_DRIFT)
            unmapped_count = sum(
                1 for d in diffs if d.kind == MISMATCH_UNMAPPED_ACCOUNT
            )
            mismatch_payload = [d.to_dict() for d in diffs if d.kind != MISMATCH_MATCH]

            await self._ensure_active_rogue_keys(repo)

            run_row = await repo.insert_run(
                started_at=started_at,
                finished_at=finished_at,
                timed_out=timed_out,
                error=error,
                broker_line_count=len(broker_lines),
                match_count=match_count,
                ghost_count=ghost_count,
                orphan_count=orphan_count,
                drift_count=drift_count,
                unmapped_account_count=unmapped_count,
                mismatches=mismatch_payload,
            )

            event_repo = EventRepository(session)

            await event_repo.append(
                process="reconcile",
                kind="POSITION_RECONCILE",
                detail={
                    "run_id": run_row.id,
                    "timed_out": timed_out,
                    "error": error,
                    "broker_line_count": len(broker_lines),
                    "match_count": match_count,
                    "ghost_count": ghost_count,
                    "orphan_count": orphan_count,
                    "drift_count": drift_count,
                    "unmapped_account_count": unmapped_count,
                    "mismatches": mismatch_payload,
                },
                idempotency_key=f"reconcile:{run_row.id}",
            )

            # Authoritative active rogue trades tracking (transition-based)
            current_rogues: dict[RogueKey, ReconcileDiff] = {}
            for d in diffs:
                if d.kind in (
                    MISMATCH_QTY_DRIFT,
                    MISMATCH_BROKER_ORPHAN,
                    MISMATCH_LEDGER_GHOST,
                ):
                    acc = str(d.ibkr_account or d.account_id or "")
                    rogue_key: RogueKey = (acc, d.symbol, d.sec_type, d.kind)
                    current_rogues[rogue_key] = d

            active_keys: dict[RogueKey, dict[str, Any]] = (
                self._active_rogue_keys if self._active_rogue_keys is not None else {}
            )
            new_keys: set[RogueKey] = set(current_rogues.keys()) - set(
                active_keys.keys()
            )
            resolved_keys: set[RogueKey] = set(active_keys.keys()) - set(
                current_rogues.keys()
            )

            for key in sorted(new_keys):
                diff = current_rogues[key]
                # Include canonical message for notification center / Telegram
                det_msg = (
                    f"🚨 ROGUE TRADE DETECTED: {diff.kind} on {diff.symbol} "
                    f"({diff.ibkr_account or diff.account_id}). "
                    f"Broker qty: {diff.broker_qty}, Ledger qty: {diff.ledger_qty}"
                )
                det_payload: dict[str, Any] = {
                    "rogue_type": diff.kind,
                    "symbol": diff.symbol,
                    "sec_type": diff.sec_type,
                    "con_id": diff.con_id,
                    "account_id": diff.account_id,
                    "ibkr_account": diff.ibkr_account,
                    "broker_qty": diff.broker_qty,
                    "ledger_qty": diff.ledger_qty,
                    "in_flight": diff.in_flight,
                    "run_id": run_row.id,
                    "message": det_msg,
                }
                idempotency_key = (
                    f"rogue_det:{run_row.id}:{key[0]}:{key[1]}:{key[2]}:{key[3]}"
                )
                await event_repo.append(
                    process="reconcile",
                    kind="ROGUE_TRADE_DETECTED",
                    detail=det_payload,
                    idempotency_key=idempotency_key,
                )
                # Non-blocking Telegram dispatch — log failures via done callback
                _task = asyncio.create_task(
                    send_canonical_telegram("ROGUE_TRADE_DETECTED", det_payload)
                )
                _task.add_done_callback(
                    lambda t: logger.debug(
                        "ROGUE_TRADE_DETECTED telegram done: %s", t.exception()
                    )
                    if t.exception()
                    else None
                )
                active_keys[key] = det_payload

            for key in sorted(resolved_keys):
                prev_payload = active_keys[key]
                res_symbol = str(prev_payload.get("symbol", key[1]))
                res_account = prev_payload.get("ibkr_account") or prev_payload.get(
                    "account_id"
                )
                res_msg = f"🟢 ROGUE TRADE RESOLVED: {prev_payload.get('rogue_type', key[3])} on {res_symbol} ({res_account})"
                res_payload: dict[str, Any] = {
                    "rogue_type": prev_payload.get("rogue_type", key[3]),
                    "symbol": prev_payload.get("symbol", key[1]),
                    "sec_type": prev_payload.get("sec_type", key[2]),
                    "con_id": prev_payload.get("con_id"),
                    "account_id": prev_payload.get("account_id"),
                    "ibkr_account": prev_payload.get("ibkr_account"),
                    "run_id": run_row.id,
                    "message": res_msg,
                }
                idempotency_key = (
                    f"rogue_res:{run_row.id}:{key[0]}:{key[1]}:{key[2]}:{key[3]}"
                )
                await event_repo.append(
                    process="reconcile",
                    kind="ROGUE_TRADE_RESOLVED",
                    detail=res_payload,
                    idempotency_key=idempotency_key,
                )
                _task = asyncio.create_task(
                    send_canonical_telegram("ROGUE_TRADE_RESOLVED", res_payload)
                )
                _task.add_done_callback(
                    lambda t: logger.debug(
                        "ROGUE_TRADE_RESOLVED telegram done: %s", t.exception()
                    )
                    if t.exception()
                    else None
                )
                del active_keys[key]

            self._active_rogue_keys = active_keys

        if mismatch_payload or timed_out or error:
            logger.warning(
                "Position reconcile run_id=%s broker_lines=%d match=%d ghost=%d orphan=%d drift=%d unmapped=%d timed_out=%s error=%s",
                run_row.id,
                len(broker_lines),
                match_count,
                ghost_count,
                orphan_count,
                drift_count,
                unmapped_count,
                timed_out,
                error,
            )
        else:
            logger.info(
                "Position reconcile run_id=%s broker_lines=%d all matched",
                run_row.id,
                len(broker_lines),
            )


async def fetch_in_flight_accounts(session: AsyncSession) -> set[int]:
    """Accounts with active baskets, processing signal jobs, or active manual orders."""
    basket_rows = (
        (
            await session.execute(
                select(BasketModel.account_id).where(
                    BasketModel.state.in_(["EXECUTING", "UNWINDING"])
                )
            )
        )
        .scalars()
        .all()
    )
    job_rows = (
        (
            await session.execute(
                select(SignalJobModel.account_scope).where(
                    SignalJobModel.status == JOB_STATUS_PROCESSING
                )
            )
        )
        .scalars()
        .all()
    )
    manual_order_rows = (
        (
            await session.execute(
                select(ManualOrderModel.account_id).where(
                    ManualOrderModel.status.in_(
                        ["PENDING_SUBMIT", "SUBMITTED", "PARTIALLY_FILLED"]
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    accounts: set[int] = set(basket_rows) | set(manual_order_rows)
    for scope in job_rows:
        if scope is None:
            continue
        try:
            accounts.add(int(scope))
        except (TypeError, ValueError):
            continue
    return accounts
