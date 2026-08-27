"""Periodic IBKR broker-vs-ledger position reconciliation (snapshot + log only)."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.broker.ibkr.positions import BrokerPositionLine
from app.db.models.account import AccountModel
from app.db.models.basket import BasketModel
from app.db.models.instrument import InstrumentModel
from app.db.models.position import PositionModel
from app.db.models.signal import JOB_STATUS_PROCESSING, SignalJobModel
from app.db.repositories.broker_position_repository import BrokerPositionRepository
from app.db.repositories.event_repository import EventRepository

logger = logging.getLogger(__name__)

RECONCILE_INTERVAL_SEC = 30.0
POSITIONS_REQUEST_TIMEOUT_SEC = 15.0
QTY_EPSILON = 1e-6

MISMATCH_MATCH = "MATCH"
MISMATCH_LEDGER_GHOST = "LEDGER_GHOST"
MISMATCH_BROKER_ORPHAN = "BROKER_ORPHAN"
MISMATCH_QTY_DRIFT = "QTY_DRIFT"
MISMATCH_UNMAPPED_ACCOUNT = "UNMAPPED_ACCOUNT"


@dataclass(frozen=True)
class LedgerNetLine:
    account_id: int
    symbol: str
    sec_type: str
    signed_qty: Decimal
    con_ids: frozenset[int]


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
        }


def _qty_close(a: float | Decimal | None, b: float | Decimal | None) -> bool:
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= QTY_EPSILON


def _norm_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def _norm_sec_type(sec_type: str) -> str:
    return sec_type.strip().upper()


def build_ledger_net_lines(
    open_rows: list[PositionModel],
    instruments: list[InstrumentModel],
) -> list[LedgerNetLine]:
    """Net OPEN ledger qty per (account_id, symbol) from pair legs."""
    symbol_to_conids: dict[tuple[str, str], set[int]] = defaultdict(set)
    for inst in instruments:
        key = (_norm_symbol(inst.symbol), _norm_sec_type(inst.sec_type))
        symbol_to_conids[key].add(int(inst.trade_conid))

    nets: dict[tuple[int, str, str], Decimal] = defaultdict(lambda: Decimal(0))

    for row in open_rows:
        legs = [
            (row.leg_a_symbol, getattr(row, "leg_a_instrument_type", "STK"), row.leg_a_signed_qty),
            (row.leg_b_symbol, getattr(row, "leg_b_instrument_type", None), row.leg_b_signed_qty),
        ]
        for symbol, inst_type, signed_qty in legs:
            if not symbol or signed_qty is None:
                continue
            sec_type = _norm_sec_type(inst_type or "STK")
            key = (row.account_id, _norm_symbol(symbol), sec_type)
            nets[key] += Decimal(str(signed_qty))

    result: list[LedgerNetLine] = []
    for (account_id, symbol, sec_type), qty in nets.items():
        if abs(float(qty)) <= QTY_EPSILON:
            continue
        con_ids = symbol_to_conids.get((symbol, sec_type), set())
        result.append(
            LedgerNetLine(
                account_id=account_id,
                symbol=symbol,
                sec_type=sec_type,
                signed_qty=qty,
                con_ids=frozenset(con_ids),
            )
        )
    return result


def classify_reconcile_diffs(
    *,
    broker_lines: list[BrokerPositionLine],
    ledger_lines: list[LedgerNetLine],
    ibkr_to_account: dict[str, int],
    timed_out: bool,
    in_flight_accounts: set[int],
) -> list[ReconcileDiff]:
    """Compare broker snapshot to OPEN ledger nets. Read-only classification."""
    broker_by_key: dict[tuple[int, str, str], list[BrokerPositionLine]] = defaultdict(list)
    unmapped: list[BrokerPositionLine] = []

    for line in broker_lines:
        account_id = ibkr_to_account.get(line.ibkr_account)
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
        broker_qty = sum(line.quantity for line in broker_group) if broker_group else None
        ledger_qty = float(ledger.signed_qty) if ledger is not None else None
        in_flight = account_id in in_flight_accounts
        ibkr_account = next(
            (line.ibkr_account for line in broker_group),
            None,
        )
        con_id = broker_group[0].con_id if broker_group else (
            next(iter(ledger.con_ids), None) if ledger is not None else None
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
    ) -> None:
        self._session_factory = session_factory
        self._client = client
        self._interval_sec = interval_sec
        self._request_timeout_sec = request_timeout_sec
        self._task: asyncio.Task | None = None
        self._running = False
        self._sweep_lock = asyncio.Lock()

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
            logger.debug("Position reconcile sweep skipped: previous sweep still running")
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
                request_async = getattr(self._client, "request_positions_async", None)
                if callable(request_async):
                    broker_lines, timed_out = await request_async(
                        timeout=self._request_timeout_sec
                    )
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
            accounts = list((await session.execute(select(AccountModel))).scalars().all())
            ibkr_to_account = {acc.ibkr_account: acc.id for acc in accounts}

            snapshot_rows = [
                {
                    "ibkr_account": line.ibkr_account,
                    "con_id": line.con_id,
                    "account_id": ibkr_to_account.get(line.ibkr_account),
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
                ).scalars().all()
            )
            instruments = list(
                (await session.execute(select(InstrumentModel))).scalars().all()
            )
            in_flight_accounts = await fetch_in_flight_accounts(session)

            ledger_lines = build_ledger_net_lines(open_rows, instruments)
            diffs = classify_reconcile_diffs(
                broker_lines=broker_lines,
                ledger_lines=ledger_lines,
                ibkr_to_account=ibkr_to_account,
                timed_out=timed_out,
                in_flight_accounts=in_flight_accounts,
            )

            match_count = sum(1 for d in diffs if d.kind == MISMATCH_MATCH)
            ghost_count = sum(1 for d in diffs if d.kind == MISMATCH_LEDGER_GHOST)
            orphan_count = sum(1 for d in diffs if d.kind == MISMATCH_BROKER_ORPHAN)
            drift_count = sum(1 for d in diffs if d.kind == MISMATCH_QTY_DRIFT)
            unmapped_count = sum(1 for d in diffs if d.kind == MISMATCH_UNMAPPED_ACCOUNT)
            mismatch_payload = [d.to_dict() for d in diffs if d.kind != MISMATCH_MATCH]

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

            await EventRepository(session).append(
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
    """Accounts with active baskets or processing signal jobs."""
    basket_rows = (
        await session.execute(
            select(BasketModel.account_id).where(
                BasketModel.state.in_(["EXECUTING", "UNWINDING"])
            )
        )
    ).scalars().all()
    job_rows = (
        await session.execute(
            select(SignalJobModel.account_scope).where(
                SignalJobModel.status == JOB_STATUS_PROCESSING
            )
        )
    ).scalars().all()
    accounts: set[int] = set(basket_rows)
    for scope in job_rows:
        if scope is None:
            continue
        try:
            accounts.add(int(scope))
        except (TypeError, ValueError):
            continue
    return accounts
