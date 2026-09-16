"""Durable Kill Switch / Emergency Flatten Service.

Orchestrates non-blocking, idempotent, bounded parallel emergency position flattening,
partial-fill aware retries, and authoritative broker reconciliation.
"""

import asyncio
import inspect
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.account import AccountModel
from app.db.models.kill_switch import (
    KILL_SWITCH_STATUS_ACTIVATING,
    KILL_SWITCH_STATUS_CLEARED,
    KILL_SWITCH_STATUS_COMPLETE,
    KILL_SWITCH_STATUS_FLAT,
    KILL_SWITCH_STATUS_FLATTENING,
    KILL_SWITCH_STATUS_RECONCILING,
    KILL_SWITCH_STATUS_RETRYING,
    KILL_SWITCH_STATUS_UNRESOLVED,
    KillSwitchOperationModel,
)
from app.db.models.position import PositionModel
from app.db.repositories.position_repository import PositionRepository
from app.rms.models import (
    ExecutionIntentMode,
    OrderAction,
    OrderIntent,
    OrderLeg,
)
from app.rms.models import OrderSide as RMSOrderSide

logger = logging.getLogger(__name__)


def _kill_switch_order_trade_ids(trade_id: str) -> tuple[str, ...]:
    """Order rows for a flatten basket use ``KILLSWITCH-{trade_id}`` as trade_id."""
    if trade_id.startswith("KILLSWITCH-"):
        return (trade_id,)
    return (trade_id, f"KILLSWITCH-{trade_id}")


def _is_kill_switch_close_order(order: Any) -> bool:
    internal_id = order.internal_order_id or ""
    if getattr(order, "is_compensation", False) or ":UNWIND:" in internal_id:
        return False
    return "KILLSWITCH-" in internal_id or ":CLOSE" in internal_id

# In-memory cache of accounts blocked from opening new positions. This is a
# read cache only -- kill_switch_operations is authoritative, and the cache is
# rebuilt from it on startup. Never mutate this set directly: use
# _arm_kill_switch_cache / clear_account_kill_switch so the DB stays in step.
_KILL_SWITCH_ACTIVE_ACCOUNTS: set[int] = set()

# Statuses that leave an account armed. Completing a flatten does NOT disarm:
# only an explicit operator clear moves an operation to CLEARED.
_ARMED_STATUSES = (
    KILL_SWITCH_STATUS_ACTIVATING,
    KILL_SWITCH_STATUS_FLATTENING,
    KILL_SWITCH_STATUS_RECONCILING,
    KILL_SWITCH_STATUS_RETRYING,
    KILL_SWITCH_STATUS_FLAT,
    KILL_SWITCH_STATUS_COMPLETE,
    KILL_SWITCH_STATUS_UNRESOLVED,
)


def is_account_kill_switch_active(account_id: int) -> bool:
    """Return True if account is currently in active emergency kill-switch mode."""
    return account_id in _KILL_SWITCH_ACTIVE_ACCOUNTS


async def get_armed_kill_switch_operation(
    session: AsyncSession, account_id: int
) -> KillSwitchOperationModel | None:
    """Latest armed operation for an account, or None if disarmed in the DB."""
    result = await session.execute(
        select(KillSwitchOperationModel)
        .where(
            KillSwitchOperationModel.account_id == account_id,
            KillSwitchOperationModel.status.in_(_ARMED_STATUSES),
        )
        .order_by(KillSwitchOperationModel.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


def _arm_kill_switch_cache(account_id: int) -> None:
    """Mark an account blocked in the in-memory cache."""
    _KILL_SWITCH_ACTIVE_ACCOUNTS.add(account_id)


def clear_account_kill_switch_cache(account_id: int) -> None:
    """Remove an account from the in-memory blocked-account cache."""
    _KILL_SWITCH_ACTIVE_ACCOUNTS.discard(account_id)


async def hydrate_kill_switch_cache(
    session_factory: async_sessionmaker[AsyncSession],
) -> set[int]:
    """Rebuild the blocked-account cache from Postgres.

    Must run before any signal is processed. The cache previously lived only in
    process memory with no rehydration, so a restart silently disarmed every
    active kill switch and OPENs resumed on halted accounts.
    """
    async with session_factory() as session:
        result = await session.execute(
            select(KillSwitchOperationModel.account_id)
            .where(KillSwitchOperationModel.status.in_(_ARMED_STATUSES))
            .distinct()
        )
        armed = {int(row[0]) for row in result.all()}

    _KILL_SWITCH_ACTIVE_ACCOUNTS.clear()
    _KILL_SWITCH_ACTIVE_ACCOUNTS.update(armed)
    if armed:
        logger.warning(
            "KILL SWITCH REARMED FROM DB: %d account(s) blocked from new OPENs: %s",
            len(armed),
            sorted(armed),
        )
    else:
        logger.info("Kill switch cache hydrated: no accounts armed")
    return armed


async def clear_account_kill_switch(
    session_factory: async_sessionmaker[AsyncSession],
    account_id: int,
    *,
    cleared_by: str = "operator",
) -> int:
    """Explicitly disarm an account, allowing new OPENs again.

    Returns the number of operations moved to CLEARED. The DB write happens
    first: if it fails the account stays blocked, which is the safe direction.
    """
    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        result = await session.execute(
            update(KillSwitchOperationModel)
            .where(
                KillSwitchOperationModel.account_id == account_id,
                KillSwitchOperationModel.status.in_(_ARMED_STATUSES),
            )
            .values(
                status=KILL_SWITCH_STATUS_CLEARED,
                cleared_at=now,
                cleared_by=cleared_by,
            )
        )
        count = int(result.rowcount or 0)  # type: ignore[attr-defined]

    _KILL_SWITCH_ACTIVE_ACCOUNTS.discard(account_id)
    logger.warning(
        "KILL SWITCH CLEARED: account_id=%s operations=%d cleared_by=%s",
        account_id,
        count,
        cleared_by,
    )
    return count


class KillSwitchService:
    """Service managing durable emergency flatten operations."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        order_manager: Any | None = None,
        max_concurrent_positions: int = 5,
    ) -> None:
        self._session_factory = session_factory
        self._order_manager = order_manager
        self._max_concurrent_positions = max_concurrent_positions
        self._semaphore = asyncio.Semaphore(max_concurrent_positions)
        self._in_flight: dict[UUID, asyncio.Task[None]] = {}

    async def initiate_square_off(
        self, account_id: int, requested_by: str = "operator"
    ) -> tuple[KillSwitchOperationModel, bool]:
        """Atomically create a new KillSwitchOperation or return existing active operation.

        Returns:
            (operation, created_new_bool)
        """
        async with self._session_factory() as session, session.begin():
            account = await session.get(AccountModel, account_id)
            if account is None:
                raise ValueError(f"Account {account_id} not found.")

            # Check for existing active operation to enforce strict idempotency
            stmt = select(KillSwitchOperationModel).where(
                KillSwitchOperationModel.account_id == account_id,
                KillSwitchOperationModel.status.in_(_ARMED_STATUSES),
            )
            result = await session.execute(stmt)
            existing_op = result.scalars().first()
            if existing_op is not None:
                logger.info(
                    "Kill Switch activation requested for account_id=%s (%s), returning existing operation_id=%s status=%s",
                    account_id,
                    account.ibkr_account,
                    existing_op.operation_id,
                    existing_op.status,
                )
                return existing_op, False

            # Query open positions for account
            pos_result = await session.execute(
                select(PositionModel).where(
                    PositionModel.account_id == account_id,
                    PositionModel.risk_state == "OPEN",
                )
            )
            open_positions = pos_result.scalars().all()

            operation = KillSwitchOperationModel(
                operation_id=uuid4(),
                account_id=account_id,
                ibkr_account=account.ibkr_account,
                status=KILL_SWITCH_STATUS_ACTIVATING,
                requested_by=requested_by,
                initial_position_count=len(open_positions),
                flattened_count=0,
                working_count=0,
                retrying_count=0,
                unresolved_count=0,
                final_exposure=0.0,
            )
            session.add(operation)

            # Block NEW opening signals for this account
            _arm_kill_switch_cache(account_id)

        logger.warning(
            "EMERGENCY KILL SWITCH ACTIVATED: operation_id=%s account_id=%s ibkr_account=%s open_positions=%d",
            operation.operation_id,
            account_id,
            operation.ibkr_account,
            operation.initial_position_count,
        )
        return operation, True

    async def arm_account_kill_switch_only(
        self, account_id: int, requested_by: str = "emergency_webhook"
    ) -> tuple[KillSwitchOperationModel, bool]:
        """Atomically arm existing account Kill Switch without executing broker flatten orders.

        Returns:
            (operation, created_new_bool)
        """
        async with self._session_factory() as session, session.begin():
            account = await session.get(AccountModel, account_id)
            if account is None:
                raise ValueError(f"Account {account_id} not found.")

            # Check for existing active operation to enforce strict idempotency
            stmt = select(KillSwitchOperationModel).where(
                KillSwitchOperationModel.account_id == account_id,
                KillSwitchOperationModel.status.in_(_ARMED_STATUSES),
            )
            result = await session.execute(stmt)
            existing_op = result.scalars().first()
            if existing_op is not None or is_account_kill_switch_active(account_id):
                _arm_kill_switch_cache(account_id)
                if existing_op is not None:
                    logger.info(
                        "Kill Switch emergency arm requested for account_id=%s (%s), returning existing operation_id=%s status=%s",
                        account_id,
                        account.ibkr_account,
                        existing_op.operation_id,
                        existing_op.status,
                    )
                    return existing_op, False

            # Query open positions for account
            pos_result = await session.execute(
                select(PositionModel).where(
                    PositionModel.account_id == account_id,
                    PositionModel.risk_state == "OPEN",
                )
            )
            open_positions = pos_result.scalars().all()

            operation = KillSwitchOperationModel(
                operation_id=uuid4(),
                account_id=account_id,
                ibkr_account=account.ibkr_account,
                status=KILL_SWITCH_STATUS_ACTIVATING,
                requested_by=requested_by,
                initial_position_count=len(open_positions),
                flattened_count=0,
                working_count=0,
                retrying_count=0,
                unresolved_count=0,
                final_exposure=0.0,
            )
            session.add(operation)

            # Block NEW opening signals for this account
            _arm_kill_switch_cache(account_id)

        logger.warning(
            "EMERGENCY KILL SWITCH ARMED (NO BROKER FLATTEN): operation_id=%s account_id=%s ibkr_account=%s open_positions=%d",
            operation.operation_id,
            account_id,
            operation.ibkr_account,
            operation.initial_position_count,
        )
        return operation, True

    async def execute_flatten_operation_background(self, operation_id: UUID) -> None:
        """Trigger background worker task to execute non-blocking position flattening."""
        if operation_id in self._in_flight and not self._in_flight[operation_id].done():
            logger.info(
                "Kill-switch flatten already in flight operation_id=%s", operation_id
            )
            return
        task = asyncio.create_task(
            self._execute_flatten_operation(operation_id),
            name=f"kill-switch-flatten-{operation_id}",
        )
        self._in_flight[operation_id] = task

        def _clear(done: asyncio.Task[None], *, op_id: UUID = operation_id) -> None:
            self._in_flight.pop(op_id, None)
            if done.cancelled():
                return
            exc = done.exception() if not done.cancelled() else None
            if exc is not None:
                logger.exception(
                    "Kill-switch flatten task failed operation_id=%s",
                    op_id,
                    exc_info=exc,
                )

        task.add_done_callback(_clear)

    async def resume_incomplete_flattens(self) -> None:
        """Restart flatten workers for armed ops that were mid-flatten at crash (M22)."""
        resume_statuses = (
            KILL_SWITCH_STATUS_ACTIVATING,
            KILL_SWITCH_STATUS_FLATTENING,
            KILL_SWITCH_STATUS_RECONCILING,
        )
        async with self._session_factory() as session:
            result = await session.execute(
                select(KillSwitchOperationModel).where(
                    KillSwitchOperationModel.status.in_(resume_statuses)
                )
            )
            ops = list(result.scalars().all())
        for op in ops:
            logger.warning(
                "Resuming kill-switch flatten operation_id=%s account_id=%s status=%s",
                op.operation_id,
                op.account_id,
                op.status,
            )
            await self.execute_flatten_operation_background(op.operation_id)

    async def retry_unresolved_operations(self) -> int:
        """Durable eventual-convergence retry for UNRESOLVED ops.

        Called periodically from PositionReconciler.after_sweep (every 30s) and
        at startup. If a previous Tier2 saw INCOMPLETE_EXIT_MARKS because
        execution arrived after Tier2, this will re-run Tier2 and close the
        engine ledger when fills are now eligible. Idempotent and safe to call
        concurrently; reuses existing _reconcile_and_finalize logic which only
        mutates via PositionRepository.close_trade (row-level lock, idempotent
        via risk_state check).
        """
        retried = 0
        async with self._session_factory() as session:
            result = await session.execute(
                select(KillSwitchOperationModel).where(
                    KillSwitchOperationModel.status == KILL_SWITCH_STATUS_UNRESOLVED
                )
            )
            unresolved_ops = list(result.scalars().all())
        for op in unresolved_ops:
            # Skip if already being flattened
            if op.operation_id in self._in_flight and not self._in_flight[op.operation_id].done():
                continue
            logger.info(
                "Retrying UNRESOLVED kill-switch operation_id=%s account_id=%s",
                op.operation_id,
                op.account_id,
            )
            try:
                await self._reconcile_and_finalize(op.operation_id, op.account_id, [])
                retried += 1
            except Exception:
                logger.exception(
                    "Retry of UNRESOLVED kill-switch operation_id=%s failed",
                    op.operation_id,
                )
        if retried:
            logger.info("Kill switch retry sweep completed: retried=%d", retried)
        return retried

    async def attempt_finalize_for_account(self, account_id: int) -> bool:
        """Re-run Tier2 for a single account when a new execution arrives.

        Can be called from execution persistence path to achieve sub-30s
        convergence without waiting for the periodic sweep. Safe to call
        outside the periodic loop; checks _in_flight to avoid overlapping
        Tier2 runs.
        """
        # Find latest UNRESOLVED or RECONCILING operation for this account
        async with self._session_factory() as session:
            result = await session.execute(
                select(KillSwitchOperationModel)
                .where(
                    KillSwitchOperationModel.account_id == account_id,
                    KillSwitchOperationModel.status.in_(
                        (KILL_SWITCH_STATUS_UNRESOLVED, KILL_SWITCH_STATUS_RECONCILING)
                    ),
                )
                .order_by(KillSwitchOperationModel.created_at.desc())
                .limit(1)
            )
            op = result.scalars().first()
        if op is None:
            return False
        if op.operation_id in self._in_flight and not self._in_flight[op.operation_id].done():
            return False
        logger.info(
            "Execution-triggered kill-switch finalize: account_id=%s operation_id=%s status=%s",
            account_id,
            op.operation_id,
            op.status,
        )
        await self._reconcile_and_finalize(op.operation_id, account_id, [])
        return True

    async def close_all_ledger_after_account_flatten(self, account_id: int, operation_id: UUID) -> int:
        """Close ledger rows after successful account-level broker flatten, ONLY with broker-flat verification.

        Safety invariant: NEVER create BROKER OPEN + LEDGER CLOSED. For account flatten,
        ledger may only be closed for a position whose legs' broker symbols are now flat
        (verified via broker_positions) AND whose execution quantities match.
        If broker still shows quantity for a symbol, that position stays OPEN/UNRESOLVED.
        If execution price missing for a leg, that position stays UNRESOLVED (no entry-mark fallback).
        """
        from app.db.models.broker_position import BrokerPositionModel
        from app.db.models.manual_order import ManualPositionModel
        from app.db.models.trade_execution import TradeExecutionModel
        from collections import defaultdict

        closed = 0
        async with self._session_factory() as session, session.begin():
            eng_rows = (await session.execute(select(PositionModel).where(PositionModel.account_id == account_id, PositionModel.risk_state == "OPEN"))).scalars().all()
            man_rows = (await session.execute(select(ManualPositionModel).where(ManualPositionModel.account_id == account_id, ManualPositionModel.status == "OPEN"))).scalars().all()

            # Broker snapshot for this account – authoritative external reality
            broker_rows = (await session.execute(select(BrokerPositionModel).where(BrokerPositionModel.account_id == account_id))).scalars().all()
            broker_qty_by_symbol: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
            for b in broker_rows:
                broker_qty_by_symbol[b.symbol.strip().upper()] += Decimal(str(b.signed_qty))

            # Recent trade_executions for price + quantity verification (last 2h)
            from datetime import timedelta

            since = datetime.now(UTC) - timedelta(hours=2)
            exec_rows = (await session.execute(select(TradeExecutionModel).where(TradeExecutionModel.account_id == account_id, TradeExecutionModel.executed_at >= since))).scalars().all()
            # Price: latest per symbol
            price_by_symbol: dict[str, Decimal] = {}
            # Quantity: sum per symbol
            exec_qty_by_symbol: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
            for e in exec_rows:
                norm = e.symbol.strip().upper()
                price_by_symbol[norm] = Decimal(str(e.price))
                exec_qty_by_symbol[norm] += Decimal(str(e.quantity)).copy_abs()

            # Engine: only close if BOTH legs broker-flat (qty 0) and execution qty covers leg qty and price available
            for p in eng_rows:
                needed = [s for s in (p.leg_a_symbol, p.leg_b_symbol) if s]
                # Broker-flat check per leg
                broker_still_open = any(broker_qty_by_symbol.get(s.strip().upper(), Decimal(0)) != 0 for s in needed)
                if broker_still_open:
                    logger.warning(
                        "Account flatten skip ledger close: broker still OPEN for trade_id=%s needed=%s broker_qty=%s",
                        p.trade_id,
                        needed,
                        {s: str(broker_qty_by_symbol.get(s.strip().upper(), Decimal(0))) for s in needed},
                    )
                    continue
                # Execution evidence check per leg
                leg_qtys = {p.leg_a_symbol.strip().upper(): abs(p.leg_a_signed_qty), p.leg_b_symbol.strip().upper(): abs(p.leg_b_signed_qty) if p.leg_b_symbol and p.leg_b_signed_qty else None}
                # Verify each needed symbol has execution qty >= leg qty and price
                missing_price = [s for s in needed if s.strip().upper() not in price_by_symbol]
                if missing_price:
                    logger.warning("Account flatten skip: missing execution price for trade_id=%s missing=%s", p.trade_id, missing_price)
                    continue
                insufficient_qty = []
                for sym in needed:
                    norm = sym.strip().upper()
                    required = leg_qtys.get(norm)
                    if required is None:
                        continue
                    if exec_qty_by_symbol.get(norm, Decimal(0)) < required - Decimal("0.0001"):
                        insufficient_qty.append(f"{sym} need {required} have {exec_qty_by_symbol.get(norm, Decimal(0))}")
                if insufficient_qty:
                    logger.warning("Account flatten skip: insufficient execution qty for trade_id=%s %s", p.trade_id, insufficient_qty)
                    continue
                exit_marks = {sym: price_by_symbol[sym.strip().upper()] for sym in needed}
                try:
                    repo = PositionRepository(session)
                    await repo.close_trade(p.trade_id, account_id=account_id, exit_marks=exit_marks)
                    from app.db.repositories.event_repository import EventRepository

                    await EventRepository(session).append(
                        process="position",
                        kind="POSITION_CLOSE",
                        detail={"account_id": account_id, "trade_id": p.trade_id, "source": "ACCOUNT_FLATTEN", "operation_id": str(operation_id), "exit_marks": {k: str(v) for k, v in exit_marks.items()}},
                        idempotency_key=f"position_close:account_flatten:{account_id}:{p.trade_id}",
                    )
                    closed += 1
                    logger.info("Account flatten closed engine position trade_id=%s exit_marks=%s", p.trade_id, {k: str(v) for k, v in exit_marks.items()})
                except Exception:
                    logger.exception("Account flatten failed to close engine trade_id=%s", p.trade_id)

            # Manual: only close if broker shows flat for that symbol
            for m in man_rows:
                norm = m.symbol.strip().upper()
                if broker_qty_by_symbol.get(norm, Decimal(0)) != 0:
                    logger.warning("Account flatten skip manual: broker still OPEN symbol=%s qty=%s trade_id=%s", m.symbol, broker_qty_by_symbol.get(norm), m.trade_id)
                    continue
                # Require execution qty covering manual qty
                if exec_qty_by_symbol.get(norm, Decimal(0)) < abs(m.signed_qty) - Decimal("0.0001"):
                    logger.warning("Account flatten skip manual: insufficient exec qty symbol=%s need %s have %s", m.symbol, abs(m.signed_qty), exec_qty_by_symbol.get(norm))
                    continue
                m.status = "CLOSED"
                m.signed_qty = Decimal(0)
                m.closed_at = datetime.now(UTC)
                closed += 1
                logger.info("Account flatten closed manual position trade_id=%s symbol=%s", m.trade_id, m.symbol)
                from app.db.repositories.event_repository import EventRepository

                await EventRepository(session).append(
                    process="manual",
                    kind="MANUAL_POSITION_CLOSE",
                    detail={"account_id": account_id, "trade_id": m.trade_id, "source": "ACCOUNT_FLATTEN", "operation_id": str(operation_id)},
                    idempotency_key=f"manual_close:account_flatten:{account_id}:{m.trade_id}",
                )
            # Update operation: if any positions remain OPEN, keep UNRESOLVED, else COMPLETE
            remaining_eng = len([p for p in eng_rows if p.risk_state == "OPEN"])  # after closes, need re-query
            # Re-query to get actual remaining
            remaining_eng_rows = (await session.execute(select(PositionModel).where(PositionModel.account_id == account_id, PositionModel.risk_state == "OPEN"))).scalars().all()
            remaining_man_rows = (await session.execute(select(ManualPositionModel).where(ManualPositionModel.account_id == account_id, ManualPositionModel.status == "OPEN"))).scalars().all()
            remaining = len(remaining_eng_rows) + len(remaining_man_rows)
        if closed:
            # Determine final status based on remaining, not just closed count
            final = KILL_SWITCH_STATUS_COMPLETE if remaining == 0 else KILL_SWITCH_STATUS_UNRESOLVED
            await self._update_operation_completion(operation_id, final_status=final, unresolved=remaining)
            logger.info("Account flatten ledger close done: closed=%s remaining=%s final=%s", closed, remaining, final)
        else:
            # No positions closed – ensure operation reflects UNRESOLVED if broker still has positions
            # Check broker still has positions
            async with self._session_factory() as s:
                br = (await s.execute(select(BrokerPositionModel).where(BrokerPositionModel.account_id == account_id))).scalars().all()
                if br:
                    await self._update_operation_completion(operation_id, final_status=KILL_SWITCH_STATUS_UNRESOLVED, unresolved=len(br))
        return closed

    async def _execute_flatten_operation(self, operation_id: UUID) -> None:
        """Execute durable flatten operation asynchronously off the HTTP request thread."""
        logger.info("Starting background flatten worker execution for operation_id=%s", operation_id)

        async with self._session_factory() as session, session.begin():
            # Lock operation row to prevent concurrent flatten workers for same op
            result = await session.execute(
                select(KillSwitchOperationModel)
                .where(KillSwitchOperationModel.operation_id == operation_id)
                .with_for_update()
            )
            op = result.scalars().first()
            if op is None or op.status in (KILL_SWITCH_STATUS_COMPLETE, KILL_SWITCH_STATUS_UNRESOLVED, KILL_SWITCH_STATUS_CLEARED):
                return
            op.status = KILL_SWITCH_STATUS_FLATTENING

            pos_result = await session.execute(
                select(PositionModel).where(
                    PositionModel.account_id == op.account_id,
                    PositionModel.risk_state == "OPEN",
                )
            )
            open_positions = pos_result.scalars().all()

        if not open_positions:
            logger.info(
                "Kill Switch flatten: no OPEN engine positions for account_id=%s operation_id=%s",
                op.account_id,
                operation_id,
            )
            await self._update_operation_completion(operation_id, final_status=KILL_SWITCH_STATUS_COMPLETE, unresolved=0)
            return

        # Observability: log exactly which engine positions are selected and why manual excluded
        for p in open_positions:
            logger.info(
                "Kill Switch engine position selected: account_id=%s trade_id=%s strategy=%s leg_a=%s qty=%s leg_b=%s qty=%s source=ENGINE ledger=positions",
                p.account_id,
                p.trade_id,
                p.strategy_id,
                p.leg_a_symbol,
                p.leg_a_signed_qty,
                p.leg_b_symbol,
                p.leg_b_signed_qty,
            )
        # Explicitly verify manual ledger is NOT flattened — count manual OPEN for audit
        try:
            async with self._session_factory() as _ms:
                from sqlalchemy import select as _sel
                from app.db.models.manual_order import ManualPositionModel

                _mres = await _ms.execute(
                    _sel(ManualPositionModel).where(
                        ManualPositionModel.account_id == op.account_id,
                        ManualPositionModel.status == "OPEN",
                    )
                )
                _manual_open = _mres.scalars().all()
                if _manual_open:
                    logger.info(
                        "Kill Switch manual positions preserved: account_id=%s manual_open_count=%d symbols=%s (engine flatten scope)",
                        op.account_id,
                        len(_manual_open),
                        [(m.symbol, str(m.signed_qty), m.trade_id) for m in _manual_open],
                    )
        except Exception:
            logger.exception("Kill Switch manual audit log failed account_id=%s", op.account_id)

        baskets_coord = getattr(self._order_manager, "_baskets", None) if self._order_manager else None

        # Emit operational progress event
        if baskets_coord:
            await baskets_coord._event(
                "KILL_SWITCH_ACTIVATED",
                {
                    "operation_id": str(operation_id),
                    "account_id": open_positions[0].account_id,
                    "total_positions": len(open_positions),
                },
            )

        # Bounded parallel execution across open positions
        tasks = [
            self._flatten_single_position(op.account_id, op.ibkr_account, pos, baskets_coord)
            for pos in open_positions
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        rebuild = getattr(self._order_manager, "rebuild_rms_from_positions", None)
        if callable(rebuild):
            try:
                rebuilt = rebuild()
                if inspect.isawaitable(rebuilt):
                    await rebuilt
            except Exception:
                logger.exception("Kill-switch RMS rebuild after flatten failed")

        # Reconcile authoritatively against PostgreSQL & Broker state
        await self._reconcile_and_finalize(operation_id, open_positions[0].account_id, results)

    async def _flatten_single_position(
        self,
        account_id: int,
        ibkr_account: str,
        pos: PositionModel,
        baskets_coord: Any | None,
    ) -> bool:
        """Flatten a single position with bounded concurrency and EMERGENCY_FLATTEN intent mode."""
        from app.services import flatten_inflight

        key = flatten_inflight.ledger_key(account_id, pos.trade_id)
        if not await flatten_inflight.try_acquire(key):
            logger.info(
                "Kill-switch flatten skipped; already flattening account_id=%s trade_id=%s",
                account_id,
                pos.trade_id,
            )
            return True
        try:
            return await self._flatten_single_position_locked(
                account_id, ibkr_account, pos, baskets_coord
            )
        finally:
            await flatten_inflight.release(key)

    async def _flatten_single_position_locked(
        self,
        account_id: int,
        ibkr_account: str,
        pos: PositionModel,
        baskets_coord: Any | None,
    ) -> bool:
        logger.info(
            "Kill Switch close authorized: account_id=%s trade_id=%s ibkr_account=%s leg_a=%s qty=%s leg_b=%s qty=%s ownership=ENGINE",
            account_id,
            pos.trade_id,
            ibkr_account,
            pos.leg_a_symbol,
            pos.leg_a_signed_qty,
            pos.leg_b_symbol,
            pos.leg_b_signed_qty,
        )
        async with self._semaphore:
            legs: list[OrderLeg] = []

            # Leg A reverse CLOSE
            if pos.leg_a_symbol and pos.leg_a_signed_qty is not None and abs(pos.leg_a_signed_qty) > 0:
                side = RMSOrderSide.SELL if pos.leg_a_signed_qty > 0 else RMSOrderSide.BUY
                qty = abs(pos.leg_a_signed_qty)
                legs.append(
                    OrderLeg(
                        symbol=pos.leg_a_symbol,
                        side=side,
                        quantity=qty,  # type: ignore[arg-type]
                        price=Decimal(0),
                        instrument_type=pos.leg_a_instrument_type or "STK",
                        leg_index=0,
                    )
                )

            # Leg B reverse CLOSE
            if pos.leg_b_symbol and pos.leg_b_signed_qty is not None and abs(pos.leg_b_signed_qty) > 0:
                side = RMSOrderSide.SELL if pos.leg_b_signed_qty > 0 else RMSOrderSide.BUY
                qty = abs(pos.leg_b_signed_qty)
                legs.append(
                    OrderLeg(
                        symbol=pos.leg_b_symbol,
                        side=side,
                        quantity=qty,  # type: ignore[arg-type]
                        price=Decimal(0),
                        instrument_type=pos.leg_b_instrument_type or "STK",
                        leg_index=1,
                    )
                )

            if not legs:
                return True

            close_intent = OrderIntent(
                signal_id=f"KILLSWITCH-{pos.trade_id}",
                strategy_id=pos.strategy_id,
                action=OrderAction.CLOSE,
                legs=legs,
                account_id=account_id,
                ibkr_account=ibkr_account,
                intent_mode=ExecutionIntentMode.EMERGENCY_FLATTEN,
            )
            logger.info(
                "Kill Switch close order submitting: account_id=%s trade_id=%s close_qty=%s legs=%s",
                account_id,
                pos.trade_id,
                [(leg.symbol, leg.side.value if hasattr(leg.side, "value") else str(leg.side), str(leg.quantity)) for leg in legs],
                len(legs),
            )

            if baskets_coord is not None:
                if self._order_manager is not None:
                    close_intent = await self._order_manager._resolve_instruments(close_intent)

                from app.rms.models import RMSOutcome, RMSResult
                rms_pass = RMSResult(
                    outcome=RMSOutcome.PASS,
                    intent=close_intent,
                    original_intent=close_intent,
                    reason="KILL_SWITCH_EMERGENCY_CLOSE",
                )
                try:
                    res = await baskets_coord.execute(close_intent, rms_pass, order_type="MARKET")
                    success = getattr(res, "success", False)
                    orders = getattr(res, "orders", [])
                    logger.info(
                        "Kill Switch broker submission result: trade_id=%s success=%s orders=%d filled=%s",
                        pos.trade_id,
                        success,
                        len(orders) if orders else 0,
                        [(o.internal_order_id, o.status.value if hasattr(o.status, "value") else str(o.status), str(getattr(o, "filled_quantity", ""))) for o in (orders or [])],
                    )
                    if orders:
                        await self._persist_flatten_close_if_filled(
                            account_id, pos.trade_id, orders
                        )
                    return success
                except Exception:
                    logger.exception("Failed to execute position reduction for trade_id=%s", pos.trade_id)
                    return False
            # No basket coordinator (e.g. in tests) — still log authorized close qty for audit
            logger.warning(
                "Kill Switch no basket coordinator: trade_id=%s close would have been %s (not submitted)",
                pos.trade_id,
                [(leg.symbol, leg.side.value if hasattr(leg.side, "value") else str(leg.side), str(leg.quantity)) for leg in legs],
            )
            return True

    async def _persist_flatten_close_if_filled(
        self,
        account_id: int,
        trade_id: str,
        orders: list[Any],
    ) -> bool:
        """Close the ledger when summed remainder-retry fills match open size.

        An ERROR/REJECTED original child does not block POSITION_CLOSE if retry
        fills cover both legs. Compensation / UNWIND children are ignored.
        """
        from app.db.repositories.event_repository import EventRepository
        from app.services.model_blue.persistence import (
            _commission_from_orders,
            _exit_marks_from_orders,
            close_fills_match_open,
        )

        fill_orders = [o for o in orders if not getattr(o, "is_compensation", False)]
        if not fill_orders:
            logger.info(
                "Kill Switch skip persist close: no fill_orders account_id=%s trade_id=%s orders=%d",
                account_id,
                trade_id,
                len(orders),
            )
            return False

        async with self._session_factory() as session, session.begin():
            pos_repo = PositionRepository(session)
            p_row = await pos_repo.get_open_by_trade_id(trade_id, account_id=account_id)
            if p_row is None:
                logger.info(
                    "Kill Switch skip persist close: no OPEN row account_id=%s trade_id=%s",
                    account_id,
                    trade_id,
                )
                return False
            # Diagnostic: log authorized vs filled quantities per leg
            try:
                from app.services.model_blue.persistence import _filled_qty_by_symbol

                filled_by_symbol_dbg = _filled_qty_by_symbol(fill_orders)
                logger.info(
                    "Kill Switch persist close check: account_id=%s trade_id=%s leg_a=%s qty=%s leg_b=%s qty=%s filled_by_symbol=%s orders=%s",
                    account_id,
                    trade_id,
                    p_row.leg_a_symbol,
                    p_row.leg_a_signed_qty,
                    p_row.leg_b_symbol,
                    p_row.leg_b_signed_qty,
                    {k: str(v) for k, v in filled_by_symbol_dbg.items()},
                    [(o.internal_order_id, str(getattr(o, "filled_quantity", None)), o.status.value if hasattr(o.status, "value") else o.status) for o in fill_orders],
                )
            except Exception:
                logger.exception("Kill Switch diagnostic log failed trade_id=%s", trade_id)

            if not close_fills_match_open(
                trade_id=trade_id,
                leg_a_symbol=p_row.leg_a_symbol,
                leg_a_signed_qty=p_row.leg_a_signed_qty,
                leg_b_symbol=p_row.leg_b_symbol,
                leg_b_signed_qty=p_row.leg_b_signed_qty,
                orders=fill_orders,
            ):
                logger.warning(
                    "Kill Switch skip persist close: fill qty does not match open "
                    "account_id=%s trade_id=%s",
                    account_id,
                    trade_id,
                )
                return False
            exit_marks = _exit_marks_from_orders(fill_orders)
            needed = [s for s in (p_row.leg_a_symbol, p_row.leg_b_symbol) if s]
            # Fallback: if exit marks missing (e.g. order.average_fill_price not yet set),
            # try executions table weighted avg as secondary source before skipping.
            if any(symbol not in exit_marks for symbol in needed):
                # Attempt DB fallback via executions for this trade's orders
                try:
                    from app.db.repositories.execution_repository import ExecutionRepository
                    from app.oms.models import executions_weighted_average

                    # Build fallback marks from order executions dict if present
                    for o in fill_orders:
                        ex = getattr(o, "executions", None) or {}
                        if ex and o.symbol not in exit_marks:
                            derived = executions_weighted_average(ex)
                            if derived is not None:
                                exit_marks[o.symbol] = derived
                except Exception:
                    logger.exception("Kill Switch exit marks fallback failed trade_id=%s", trade_id)
            if any(symbol not in exit_marks for symbol in needed):
                # Last fallback: use last_fill_price if available
                for o in fill_orders:
                    if o.symbol not in exit_marks and getattr(o, "last_fill_price", None) is not None:
                        try:
                            exit_marks[o.symbol] = o.last_fill_price  # type: ignore[assignment]
                        except Exception:
                            continue
            if any(symbol not in exit_marks for symbol in needed):
                logger.warning(
                    "INCOMPLETE_EXIT_MARKS: skip KS persist close trade_id=%s missing=%s exit_marks_keys=%s",
                    trade_id,
                    [s for s in needed if s not in exit_marks],
                    list(exit_marks.keys()),
                )
                return False
            comm = _commission_from_orders(fill_orders)
            await pos_repo.close_trade(
                trade_id,
                account_id=account_id,
                exit_marks=exit_marks,
                commission=comm,
            )
            await EventRepository(session).append(
                process="position",
                kind="POSITION_CLOSE",
                detail={
                    "account_id": account_id,
                    "trade_id": trade_id,
                    "source": "KILL_SWITCH",
                    "exit_marks": {k: str(v) for k, v in exit_marks.items()},
                    "filled_orders": [o.internal_order_id for o in fill_orders],
                },
                idempotency_key=f"position_close:kill_switch:{account_id}:{trade_id}",
            )
            logger.info(
                "Kill Switch persisted position close: account_id=%d trade_id=%s exit_marks=%s",
                account_id,
                trade_id,
                {k: str(v) for k, v in exit_marks.items()},
            )

        live = getattr(self._order_manager, "_live_pnl", None)
        if live is not None:
            live.unwatch(account_id, trade_id)
        return True

    async def _reconcile_and_finalize(
        self, operation_id: UUID, account_id: int, results: list[Any]
    ) -> None:
        """Reconcile final exposure from database and broker state before setting operation COMPLETE.

        Tier 2 auto-repair: for any engine OPEN row whose KILLSWITCH close orders are FILLED
        in the DB with matching qty, close the ledger. Manual positions (manual_positions)
        are intentionally NOT inspected here — engine flatten must never mutate manual ledger.
        """
        # Tier 2 Reconciliation: Auto-repair any open positions whose close orders filled in DB
        async with self._session_factory() as session, session.begin():
            pos_repo = PositionRepository(session)
            from app.db.repositories.execution_repository import ExecutionRepository
            from app.db.repositories.order_repository import OrderRepository
            order_repo = OrderRepository(session)
            exec_repo = ExecutionRepository(session)
            open_positions = await pos_repo.list_open()
            account_open = [p for p in open_positions if p.account_id == account_id]
            logger.info(
                "Kill Switch Tier2 reconcile: account_id=%s open_engine_positions=%d operation_id=%s",
                account_id,
                len(account_open),
                operation_id,
            )
            for pos in account_open:
                pos_orders: list[Any] = []
                seen_oids: set[str] = set()
                for tid in _kill_switch_order_trade_ids(pos.trade_id):
                    for row in await order_repo.list_by_trade_id(tid):
                        key = row.internal_order_id or str(row.id)
                        if key in seen_oids:
                            continue
                        seen_oids.add(key)
                        pos_orders.append(row)
                close_orders = [o for o in pos_orders if _is_kill_switch_close_order(o)]
                if not close_orders:
                    logger.info(
                        "Kill Switch Tier2 skip: no close orders for trade_id=%s account_id=%s",
                        pos.trade_id,
                        account_id,
                    )
                    continue
                filled_close = [o for o in close_orders if o.status == "FILLED"]
                req_legs = 2 if pos.leg_b_symbol else 1
                logger.info(
                    "Kill Switch Tier2 candidate: trade_id=%s close_orders=%d filled_close=%d req_legs=%d filled_ids=%s",
                    pos.trade_id,
                    len(close_orders),
                    len(filled_close),
                    req_legs,
                    [o.internal_order_id for o in filled_close],
                )
                if len(filled_close) >= req_legs:
                    exit_marks: dict[str, Decimal] = {}
                    filled_by_symbol: dict[str, Decimal] = {}
                    for co in filled_close:
                        if co.fill_price is not None and co.symbol:
                            exit_marks[co.symbol] = Decimal(str(co.fill_price))
                        # Execution table fallback for fill_price
                        if co.symbol and co.symbol not in exit_marks:
                            try:
                                exec_rows = await exec_repo.list_by_internal_order_id(co.internal_order_id)
                                if exec_rows:
                                    from app.db.repositories.execution_repository import weighted_average_price

                                    wavg = weighted_average_price(exec_rows)
                                    if wavg is not None:
                                        exit_marks[co.symbol] = wavg
                            except Exception:
                                logger.exception("Kill Switch exec fallback failed trade_id=%s", pos.trade_id)
                        qty = Decimal(str(
                            getattr(co, "fill_qty", None)
                            or getattr(co, "filled_quantity", None)
                            or 0
                        ))
                        # Fallback quantity from executions if order fill_qty is 0
                        if qty <= 0:
                            try:
                                exec_rows = await exec_repo.list_by_internal_order_id(co.internal_order_id)
                                qty = sum((Decimal(str(r.quantity)) for r in exec_rows), Decimal(0))
                            except Exception:
                                qty = Decimal(0)
                        if co.symbol and qty > 0:
                            filled_by_symbol[co.symbol] = (
                                filled_by_symbol.get(co.symbol, Decimal(0)) + qty
                            )
                    needed = [s for s in (pos.leg_a_symbol, pos.leg_b_symbol) if s]
                    # If still missing exit marks, try executions again for each needed symbol
                    for sym in needed:
                        if sym not in exit_marks:
                            for co in filled_close:
                                if co.symbol == sym:
                                    try:
                                        exec_rows = await exec_repo.list_by_internal_order_id(co.internal_order_id)
                                        if exec_rows:
                                            from app.db.repositories.execution_repository import weighted_average_price

                                            wavg = weighted_average_price(exec_rows)
                                            if wavg is not None:
                                                exit_marks[sym] = wavg
                                                break
                                    except Exception:
                                        continue
                    if any(symbol not in exit_marks for symbol in needed):
                        logger.warning(
                            "INCOMPLETE_EXIT_MARKS: skip KS reconcile close trade_id=%s missing=%s exit_marks=%s filled_by_symbol=%s",
                            pos.trade_id,
                            [s for s in needed if s not in exit_marks],
                            list(exit_marks.keys()),
                            {k: str(v) for k, v in filled_by_symbol.items()},
                        )
                        continue
                    from app.services.model_blue.parser import (
                        ModelBlueValidationError,
                    )
                    from app.services.model_blue.persistence import (
                        assert_close_qty_matches_open,
                    )

                    try:
                        assert_close_qty_matches_open(
                            trade_id=pos.trade_id,
                            leg_a_symbol=pos.leg_a_symbol,
                            leg_a_signed_qty=pos.leg_a_signed_qty,
                            leg_b_symbol=pos.leg_b_symbol,
                            leg_b_signed_qty=pos.leg_b_signed_qty,
                            filled_by_symbol=filled_by_symbol,
                        )
                    except ModelBlueValidationError as exc:
                        logger.warning("Skip KS reconcile close: %s filled_by_symbol=%s", exc, filled_by_symbol)
                        continue
                    await pos_repo.close_trade(
                        pos.trade_id,
                        account_id=account_id,
                        exit_marks=exit_marks,
                    )
                    logger.info(
                        "Reconciled stale position to CLOSED during Kill Switch: trade_id=%s exit_marks=%s filled_by_symbol=%s",
                        pos.trade_id,
                        {k: str(v) for k, v in exit_marks.items()},
                        {k: str(v) for k, v in filled_by_symbol.items()},
                    )
                else:
                    logger.info(
                        "Kill Switch Tier2 skip: insufficient filled legs trade_id=%s filled=%d req=%d",
                        pos.trade_id,
                        len(filled_close),
                        req_legs,
                    )

        async with self._session_factory() as session:
            remaining_positions = await PositionRepository(session).list_open()
            account_remaining = [p for p in remaining_positions if p.account_id == account_id]

        net_unresolved = len(account_remaining)
        final_status = KILL_SWITCH_STATUS_COMPLETE if net_unresolved == 0 else KILL_SWITCH_STATUS_UNRESOLVED

        await self._update_operation_completion(
            operation_id, final_status=final_status, unresolved=net_unresolved
        )

        baskets_coord = getattr(self._order_manager, "_baskets", None) if self._order_manager else None
        if baskets_coord:
            event_type = "KILL_SWITCH_COMPLETED" if final_status == KILL_SWITCH_STATUS_COMPLETE else "KILL_SWITCH_UNRESOLVED"
            await baskets_coord._event(
                event_type,
                {
                    "operation_id": str(operation_id),
                    "account_id": account_id,
                    "final_unresolved": net_unresolved,
                    "status": final_status,
                },
            )

        logger.info(
            "Kill Switch operation_id=%s finalized with status=%s unresolved_count=%d",
            operation_id,
            final_status,
            net_unresolved,
        )

    async def _update_operation_completion(
        self, operation_id: UUID, final_status: str, unresolved: int
    ) -> None:
        async with self._session_factory() as session, session.begin():
            # Lock row to prevent stale worker overwriting newer COMPLETE with UNRESOLVED (last-writer-wins)
            result = await session.execute(
                select(KillSwitchOperationModel)
                .where(KillSwitchOperationModel.operation_id == operation_id)
                .with_for_update()
            )
            op = result.scalars().first()
            if op is not None:
                # Terminal state protection: COMPLETE must never regress to UNRESOLVED/FLATTENING
                if op.status == KILL_SWITCH_STATUS_COMPLETE and final_status != KILL_SWITCH_STATUS_COMPLETE:
                    logger.warning(
                        "Kill Switch stale worker attempted regression: operation_id=%s current=%s attempted=%s — blocked",
                        operation_id,
                        op.status,
                        final_status,
                    )
                    return
                # CLEARED is operator disarm, never regress
                if op.status == KILL_SWITCH_STATUS_CLEARED:
                    return
                op.status = final_status
                op.unresolved_count = unresolved
                op.flattened_count = max(0, op.initial_position_count - unresolved)
                op.updated_at = datetime.now(UTC)
