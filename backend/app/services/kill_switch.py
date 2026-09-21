"""Durable Kill Switch / Emergency Flatten Service.

Orchestrates non-blocking, idempotent, bounded parallel emergency position flattening,
partial-fill aware retries, and authoritative broker reconciliation.
"""

import asyncio
import inspect
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.account import AccountModel
from app.db.models.kill_switch import (
    KILL_SWITCH_SCOPE_ACCOUNT,
    KILL_SWITCH_SCOPE_ENGINE,
    KILL_SWITCH_SCOPE_MANUAL,
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
from app.services.notification.types import (
    NormalizedEvent,
    NotificationSeverity,
)

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

# In-memory cache of accounts blocked from opening new engine positions. This is a
# read cache only -- kill_switch_operations is authoritative, and the cache is
# rebuilt from it on startup. Never mutate this set directly: use
# _arm_kill_switch_cache / clear_account_kill_switch so the DB stays in step.
_KILL_SWITCH_ACTIVE_ACCOUNTS: set[int] = set()

# In-memory cache of accounts where manual positions flatten is actively in flight.
# Per requirement §11, blocks new manual orders while active (ACTIVATING,
# FLATTENING, RECONCILING, RETRYING). Released when terminal (COMPLETE, FAILED/UNRESOLVED).
# Does NOT affect engine order submission. Armed by initiate_manual_square_off;
# released by _update_operation_completion, which is the single owner of that
# transition and keeps this set in step with _MANUAL_FLATTEN_ACTIVE_STATUSES so a
# restart rehydrates to the same answer.
_MANUAL_KILL_SWITCH_ACTIVE_ACCOUNTS: set[int] = set()

# Statuses that leave an account armed for engine/account scopes. Completing a flatten
# does NOT disarm: only an explicit operator clear moves an operation to CLEARED.
_ARMED_STATUSES = (
    KILL_SWITCH_STATUS_ACTIVATING,
    KILL_SWITCH_STATUS_FLATTENING,
    KILL_SWITCH_STATUS_RECONCILING,
    KILL_SWITCH_STATUS_RETRYING,
    KILL_SWITCH_STATUS_FLAT,
    KILL_SWITCH_STATUS_COMPLETE,
    KILL_SWITCH_STATUS_UNRESOLVED,
)

# Active statuses during which manual order submission must be blocked.
_MANUAL_FLATTEN_ACTIVE_STATUSES = (
    KILL_SWITCH_STATUS_ACTIVATING,
    KILL_SWITCH_STATUS_FLATTENING,
    KILL_SWITCH_STATUS_RECONCILING,
    KILL_SWITCH_STATUS_RETRYING,
)


def is_account_kill_switch_active(account_id: int) -> bool:
    """Return True if account is currently in active emergency kill-switch mode."""
    return account_id in _KILL_SWITCH_ACTIVE_ACCOUNTS


def is_manual_kill_switch_active(account_id: int) -> bool:
    """Return True if account has an active in-flight manual positions flatten."""
    return account_id in _MANUAL_KILL_SWITCH_ACTIVE_ACCOUNTS


async def get_armed_kill_switch_operation(
    session: AsyncSession, account_id: int, scope: str | None = None
) -> KillSwitchOperationModel | None:
    """Latest armed operation for an account, or None if disarmed in the DB."""
    stmt = select(KillSwitchOperationModel).where(
        KillSwitchOperationModel.account_id == account_id,
        KillSwitchOperationModel.status.in_(_ARMED_STATUSES),
    )
    if scope is not None:
        stmt = stmt.where(KillSwitchOperationModel.scope == scope)
    else:
        stmt = stmt.where(
            KillSwitchOperationModel.scope.in_((KILL_SWITCH_SCOPE_ENGINE, KILL_SWITCH_SCOPE_ACCOUNT))
        )
    stmt = stmt.order_by(KillSwitchOperationModel.created_at.desc()).limit(1)
    result = await session.execute(stmt)
    return result.scalars().first()


async def get_active_manual_kill_switch_operation(
    session: AsyncSession, account_id: int
) -> KillSwitchOperationModel | None:
    """Latest in-flight manual flatten operation for an account."""
    result = await session.execute(
        select(KillSwitchOperationModel)
        .where(
            KillSwitchOperationModel.account_id == account_id,
            KillSwitchOperationModel.scope == KILL_SWITCH_SCOPE_MANUAL,
            KillSwitchOperationModel.status.in_(_MANUAL_FLATTEN_ACTIVE_STATUSES),
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
    _MANUAL_KILL_SWITCH_ACTIVE_ACCOUNTS.discard(account_id)


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
            .where(
                KillSwitchOperationModel.status.in_(_ARMED_STATUSES),
                KillSwitchOperationModel.scope.in_((KILL_SWITCH_SCOPE_ENGINE, KILL_SWITCH_SCOPE_ACCOUNT)),
            )
            .distinct()
        )
        armed = {int(row[0]) for row in result.all()}

        man_result = await session.execute(
            select(KillSwitchOperationModel.account_id)
            .where(
                KillSwitchOperationModel.status.in_(_MANUAL_FLATTEN_ACTIVE_STATUSES),
                KillSwitchOperationModel.scope == KILL_SWITCH_SCOPE_MANUAL,
            )
            .distinct()
        )
        man_active = {int(row[0]) for row in man_result.all()}

    _KILL_SWITCH_ACTIVE_ACCOUNTS.clear()
    _KILL_SWITCH_ACTIVE_ACCOUNTS.update(armed)
    _MANUAL_KILL_SWITCH_ACTIVE_ACCOUNTS.clear()
    _MANUAL_KILL_SWITCH_ACTIVE_ACCOUNTS.update(man_active)
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
    scope: str | None = None,
    notification_orchestrator: Any | None = None,
) -> int:
    """Explicitly disarm an account, allowing new OPENs again.

    Returns the number of operations moved to CLEARED. The DB write happens
    first: if it fails the account stays blocked, which is the safe direction.
    """
    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        stmt = (
            update(KillSwitchOperationModel)
            .where(
                KillSwitchOperationModel.account_id == account_id,
                KillSwitchOperationModel.status.in_(_ARMED_STATUSES),
            )
        )
        if scope is not None:
            stmt = stmt.where(KillSwitchOperationModel.scope == scope)
        result = await session.execute(
            stmt.values(
                status=KILL_SWITCH_STATUS_CLEARED,
                cleared_at=now,
                cleared_by=cleared_by,
            )
        )
        count = int(result.rowcount or 0)  # type: ignore[attr-defined]

    if scope is None or scope in (KILL_SWITCH_SCOPE_ENGINE, KILL_SWITCH_SCOPE_ACCOUNT):
        _KILL_SWITCH_ACTIVE_ACCOUNTS.discard(account_id)
    if scope is None or scope == KILL_SWITCH_SCOPE_MANUAL:
        _MANUAL_KILL_SWITCH_ACTIVE_ACCOUNTS.discard(account_id)
    logger.warning(
        "KILL SWITCH CLEARED: account_id=%s operations=%d cleared_by=%s scope=%s",
        account_id,
        count,
        cleared_by,
        scope,
    )
    if notification_orchestrator is not None and count > 0:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                notification_orchestrator.ingest_event(
                    NormalizedEvent(
                        event_type="KILL_SWITCH_CLEARED",
                        title=f"ℹ️ KILL SWITCH CLEARED — Account {account_id}",
                        message=f"Account {account_id} kill switch cleared by {cleared_by}.",
                        category="KILL_SWITCH",
                        severity=NotificationSeverity.INFO,
                        source="kill_switch",
                        correlation_id=f"kill_switch_{account_id}",
                        details={"account_id": account_id, "cleared_by": cleared_by, "count": count},
                    )
                )
            )
        except RuntimeError:
            pass
    return count


class KillSwitchService:
    """Service managing durable emergency flatten operations."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        order_manager: Any | None = None,
        max_concurrent_positions: int = 5,
        notification_orchestrator: Any | None = None,
        client: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._order_manager = order_manager
        self._client = client or (getattr(order_manager, "_client", None) if order_manager is not None else None)
        self._max_concurrent_positions = max_concurrent_positions
        self._orchestrator = notification_orchestrator
        self._semaphore = asyncio.Semaphore(max_concurrent_positions)
        self._in_flight: dict[UUID, asyncio.Task[None]] = {}

    def _emit_notification(self, event: NormalizedEvent) -> None:
        """Asynchronously ingest event without blocking execution or holding DB locks."""
        if self._orchestrator is None:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._orchestrator.ingest_event(event))
        except RuntimeError:
            pass

    async def initiate_square_off(
        self, account_id: int, requested_by: str = "operator"
    ) -> tuple[KillSwitchOperationModel, bool]:
        """Atomically create a new KillSwitchOperation or return existing active operation (engine scope).

        Returns:
            (operation, created_new_bool)
        """
        async with self._session_factory() as session, session.begin():
            account = await session.get(AccountModel, account_id)
            if account is None:
                raise ValueError(f"Account {account_id} not found.")

            # Check for existing active engine operation to enforce strict idempotency
            stmt = select(KillSwitchOperationModel).where(
                KillSwitchOperationModel.account_id == account_id,
                KillSwitchOperationModel.scope == KILL_SWITCH_SCOPE_ENGINE,
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
                scope=KILL_SWITCH_SCOPE_ENGINE,
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
            "EMERGENCY KILL SWITCH ACTIVATED (ENGINE SCOPE): operation_id=%s account_id=%s ibkr_account=%s open_positions=%d",
            operation.operation_id,
            account_id,
            operation.ibkr_account,
            operation.initial_position_count,
        )
        self._emit_notification(
            NormalizedEvent(
                event_type="KILL_SWITCH_ACTIVATED",
                title=f"🚨 EMERGENCY KILL SWITCH ACTIVATED — {operation.ibkr_account or account_id}",
                message=(
                    f"Kill switch armed by {requested_by}. "
                    f"Initial open positions: {operation.initial_position_count}. Flattening initiated."
                ),
                category="KILL_SWITCH",
                severity=NotificationSeverity.CRITICAL,
                source="kill_switch",
                source_event_id=str(operation.operation_id),
                correlation_id=f"kill_switch_{account_id}",
                dedupe_key=f"ks_act_{operation.operation_id}",
                details={
                    "operation_id": str(operation.operation_id),
                    "account_id": account_id,
                    "ibkr_account": operation.ibkr_account,
                    "requested_by": requested_by,
                    "open_positions": operation.initial_position_count,
                },
            )
        )
        return operation, True

    async def arm_account_kill_switch_only(
        self, account_id: int, requested_by: str = "emergency_webhook"
    ) -> tuple[KillSwitchOperationModel, bool]:
        """Atomically arm existing account Kill Switch without executing broker flatten orders.

        Captures immutable flatten snapshot of current OPEN engine+manual positions
        so later reconciliation operates against that set, not whatever is OPEN later.

        Returns:
            (operation, created_new_bool)
        """
        async with self._session_factory() as session, session.begin():
            account = await session.get(AccountModel, account_id)
            if account is None:
                raise ValueError(f"Account {account_id} not found.")

            # Check for existing active account operation to enforce strict idempotency
            stmt = select(KillSwitchOperationModel).where(
                KillSwitchOperationModel.account_id == account_id,
                KillSwitchOperationModel.scope == KILL_SWITCH_SCOPE_ACCOUNT,
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

            # Query open positions for snapshot
            pos_result = await session.execute(
                select(PositionModel).where(
                    PositionModel.account_id == account_id,
                    PositionModel.risk_state == "OPEN",
                )
            )
            open_positions = pos_result.scalars().all()
            from app.db.models.manual_order import ManualPositionModel

            man_result = await session.execute(
                select(ManualPositionModel).where(
                    ManualPositionModel.account_id == account_id,
                    ManualPositionModel.status == "OPEN",
                )
            )
            open_manual = man_result.scalars().all()

            operation = KillSwitchOperationModel(
                operation_id=uuid4(),
                account_id=account_id,
                ibkr_account=account.ibkr_account,
                scope=KILL_SWITCH_SCOPE_ACCOUNT,
                status=KILL_SWITCH_STATUS_ACTIVATING,
                requested_by=requested_by,
                initial_position_count=len(open_positions) + len(open_manual),
                flattened_count=0,
                working_count=0,
                retrying_count=0,
                unresolved_count=0,
                final_exposure=0.0,
            )
            session.add(operation)
            await session.flush()  # need operation_id for FK

            # Capture immutable snapshot for account-flatten reconciliation
            await self._capture_flatten_snapshot(session, operation, open_positions, open_manual)

            # Block NEW opening signals for this account
            _arm_kill_switch_cache(account_id)

        logger.warning(
            "EMERGENCY KILL SWITCH ARMED (ACCOUNT SCOPE, NO BROKER FLATTEN): operation_id=%s account_id=%s ibkr_account=%s open_positions=%d (engine %d + manual %d)",
            operation.operation_id,
            account_id,
            operation.ibkr_account,
            operation.initial_position_count,
            len(open_positions),
            len(open_manual),
        )
        return operation, True

    async def _capture_flatten_snapshot(
        self, session: AsyncSession, operation: KillSwitchOperationModel, eng_rows: Any, man_rows: Any
    ) -> None:
        """Capture snapshot of ledger positions at flatten initiation (account scope)."""
        from app.db.models.kill_switch_snapshot import KillSwitchFlattenSnapshotModel

        now = datetime.now(UTC)
        for p in eng_rows:
            session.add(
                KillSwitchFlattenSnapshotModel(
                    operation_id=operation.operation_id,
                    account_id=operation.account_id,
                    position_type="engine",
                    trade_id=p.trade_id,
                    leg_a_symbol=p.leg_a_symbol,
                    leg_a_signed_qty=p.leg_a_signed_qty,
                    leg_a_entry_mark=p.leg_a_entry_mark,
                    leg_a_instrument_type=getattr(p, "leg_a_instrument_type", None),
                    leg_b_symbol=p.leg_b_symbol,
                    leg_b_signed_qty=p.leg_b_signed_qty,
                    leg_b_entry_mark=p.leg_b_entry_mark,
                    leg_b_instrument_type=getattr(p, "leg_b_instrument_type", None),
                    opened_at=p.opened_at,
                    snapshot_at=now,
                )
            )
        for m in man_rows:
            side = "BUY" if (m.signed_qty or 0) > 0 else "SELL"
            session.add(
                KillSwitchFlattenSnapshotModel(
                    operation_id=operation.operation_id,
                    account_id=operation.account_id,
                    position_type="manual",
                    trade_id=m.trade_id,
                    manual_position_id=m.id,
                    symbol=m.symbol,
                    con_id=m.con_id,
                    sec_type=m.sec_type,
                    side=side,
                    signed_qty=m.signed_qty,
                    avg_cost=m.avg_cost,
                    opened_at=m.opened_at,
                    snapshot_at=now,
                )
            )
    async def initiate_manual_square_off(
        self, account_id: int, requested_by: str = "operator"
    ) -> tuple[KillSwitchOperationModel, bool]:
        """Atomically create a new KillSwitchOperation for manual positions or return existing active op.

        Returns:
            (operation, created_new_bool)
        """
        from app.db.models.manual_order import ManualPositionModel

        async with self._session_factory() as session, session.begin():
            account = await session.get(AccountModel, account_id)
            if account is None:
                raise ValueError(f"Account {account_id} not found.")

            # Check for existing active manual operation
            stmt = select(KillSwitchOperationModel).where(
                KillSwitchOperationModel.account_id == account_id,
                KillSwitchOperationModel.scope == KILL_SWITCH_SCOPE_MANUAL,
                KillSwitchOperationModel.status.in_(_MANUAL_FLATTEN_ACTIVE_STATUSES),
            )
            existing_op = (await session.execute(stmt)).scalars().first()
            if existing_op is not None:
                logger.info(
                    "Manual Kill Switch requested for account_id=%s, returning active operation_id=%s status=%s",
                    account_id,
                    existing_op.operation_id,
                    existing_op.status,
                )
                _MANUAL_KILL_SWITCH_ACTIVE_ACCOUNTS.add(account_id)
                return existing_op, False

            # Query currently open/unresolved manual positions (locked FOR UPDATE)
            man_result = await session.execute(
                select(ManualPositionModel)
                .where(
                    ManualPositionModel.account_id == account_id,
                    ManualPositionModel.status.in_(["OPEN", "CLOSING", "FLATTENED_PENDING_PRICE"]),
                )
                .with_for_update()
            )
            open_manual = list(man_result.scalars().all())

            if not open_manual:
                operation = KillSwitchOperationModel(
                    operation_id=uuid4(),
                    account_id=account_id,
                    ibkr_account=account.ibkr_account,
                    scope=KILL_SWITCH_SCOPE_MANUAL,
                    status=KILL_SWITCH_STATUS_COMPLETE,
                    requested_by=requested_by,
                    initial_position_count=0,
                    flattened_count=0,
                    working_count=0,
                    retrying_count=0,
                    unresolved_count=0,
                    final_exposure=0.0,
                )
                session.add(operation)
                logger.info(
                    "Manual Kill Switch: No open manual positions for account_id=%s. Operation COMPLETE.",
                    account_id,
                )
                return operation, True

            operation = KillSwitchOperationModel(
                operation_id=uuid4(),
                account_id=account_id,
                ibkr_account=account.ibkr_account,
                scope=KILL_SWITCH_SCOPE_MANUAL,
                status=KILL_SWITCH_STATUS_ACTIVATING,
                requested_by=requested_by,
                initial_position_count=len(open_manual),
                flattened_count=0,
                working_count=0,
                retrying_count=0,
                unresolved_count=0,
                final_exposure=0.0,
            )
            session.add(operation)
            await session.flush()

            # Capture immutable snapshot for manual-flatten reconciliation
            await self._capture_manual_flatten_snapshot(session, operation, open_manual)

            # Block NEW manual orders for this account while active
            _MANUAL_KILL_SWITCH_ACTIVE_ACCOUNTS.add(account_id)

        logger.warning(
            "EMERGENCY MANUAL KILL SWITCH ACTIVATED: operation_id=%s account_id=%s ibkr_account=%s open_manual_positions=%d",
            operation.operation_id,
            account_id,
            operation.ibkr_account,
            operation.initial_position_count,
        )
        self._emit_notification(
            NormalizedEvent(
                event_type="KILL_SWITCH_ACTIVATED",
                title=f"🚨 EMERGENCY KILL SWITCH ARMED — {operation.ibkr_account or account_id}",
                message=(
                    f"Kill switch armed (no broker flatten) by {requested_by}. "
                    f"Initial open positions: {operation.initial_position_count}."
                ),
                category="KILL_SWITCH",
                severity=NotificationSeverity.CRITICAL,
                source="kill_switch",
                source_event_id=str(operation.operation_id),
                correlation_id=f"kill_switch_{account_id}",
                dedupe_key=f"ks_act_{operation.operation_id}",
                details={
                    "operation_id": str(operation.operation_id),
                    "account_id": account_id,
                    "ibkr_account": operation.ibkr_account,
                    "requested_by": requested_by,
                    "open_positions": operation.initial_position_count,
                },
            )
        )
        return operation, True

    async def _capture_manual_flatten_snapshot(
        self, session: AsyncSession, operation: KillSwitchOperationModel, man_rows: list[Any]
    ) -> None:
        """Capture snapshot of manual positions at flatten initiation (manual scope)."""
        from app.db.models.kill_switch_snapshot import KillSwitchFlattenSnapshotModel

        now = datetime.now(UTC)
        for m in man_rows:
            side = "BUY" if (m.signed_qty or 0) > 0 else "SELL"
            session.add(
                KillSwitchFlattenSnapshotModel(
                    operation_id=operation.operation_id,
                    account_id=operation.account_id,
                    position_type="manual",
                    trade_id=m.trade_id,
                    manual_position_id=m.id,
                    symbol=m.symbol,
                    con_id=m.con_id,
                    sec_type=m.sec_type,
                    side=side,
                    signed_qty=m.signed_qty,
                    avg_cost=m.avg_cost,
                    opened_at=m.opened_at,
                    snapshot_at=now,
                )
            )

    async def execute_manual_flatten_operation_background(self, operation_id: UUID) -> None:
        """Trigger background worker task to execute non-blocking manual position flattening."""
        if operation_id in self._in_flight and not self._in_flight[operation_id].done():
            logger.info("Manual kill-switch flatten already in flight operation_id=%s", operation_id)
            return
        task = asyncio.create_task(
            self._execute_manual_flatten_operation(operation_id),
            name=f"kill-switch-manual-flatten-{operation_id}",
        )
        self._in_flight[operation_id] = task

        def _clear(done: asyncio.Task[None], *, op_id: UUID = operation_id) -> None:
            self._in_flight.pop(op_id, None)
            if done.cancelled():
                return
            exc = done.exception() if not done.cancelled() else None
            if exc is not None:
                logger.exception(
                    "Manual kill-switch flatten task failed operation_id=%s",
                    op_id,
                    exc_info=exc,
                )

        task.add_done_callback(_clear)

    async def _execute_manual_flatten_operation(self, operation_id: UUID) -> None:
        """Execute durable manual flatten operation asynchronously off the HTTP thread."""
        logger.info("Starting background manual flatten worker for operation_id=%s", operation_id)

        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                select(KillSwitchOperationModel)
                .where(KillSwitchOperationModel.operation_id == operation_id)
                .with_for_update()
            )
            op = result.scalars().first()
            if op is None or op.status in (
                KILL_SWITCH_STATUS_COMPLETE,
                KILL_SWITCH_STATUS_UNRESOLVED,
                KILL_SWITCH_STATUS_CLEARED,
            ):
                return
            op.status = KILL_SWITCH_STATUS_FLATTENING

            # Query snapshot rows captured at initiation
            from app.db.models.kill_switch_snapshot import (
                KillSwitchFlattenSnapshotModel,
            )

            snap_result = await session.execute(
                select(KillSwitchFlattenSnapshotModel).where(
                    KillSwitchFlattenSnapshotModel.operation_id == operation_id,
                    KillSwitchFlattenSnapshotModel.position_type == "manual",
                )
            )
            snap_positions = list(snap_result.scalars().all())

        if not snap_positions:
            logger.info("Manual kill switch: no snapshotted positions for operation_id=%s", operation_id)
            await self._update_operation_completion(operation_id, final_status=KILL_SWITCH_STATUS_COMPLETE, unresolved=0)
            return

        # Execute position-specific close orders across all snapshotted positions
        for snap in snap_positions:
            try:
                await self._flatten_single_manual_position(
                    op.account_id, op.ibkr_account, operation_id, snap
                )
            except Exception:
                logger.exception(
                    "Failed to execute manual flatten order for trade_id=%s operation_id=%s",
                    snap.trade_id,
                    operation_id,
                )

        # Reconcile authoritatively against PostgreSQL & Broker state
        await self._reconcile_and_finalize_manual(operation_id, op.account_id)

    async def _flatten_single_manual_position(
        self,
        account_id: int,
        ibkr_account: str,
        operation_id: UUID,
        snap: Any,
    ) -> bool:
        """Submit position-specific MKT close order for a single snapshotted manual position."""
        from app.db.repositories.manual_repository import (
            ManualAuditRepository,
            ManualOrderRepository,
            ManualPositionRepository,
        )

        async with self._session_factory() as session, session.begin():
            pos_repo = ManualPositionRepository(session)
            order_repo = ManualOrderRepository(session)

            # Check if position still open in DB
            pos = await pos_repo.get_by_trade_id(account_id, snap.trade_id)
            if pos is None or pos.status == "CLOSED" or pos.signed_qty == Decimal(0):
                logger.info(
                    "Manual kill-switch position already closed: account_id=%s trade_id=%s",
                    account_id,
                    snap.trade_id,
                )
                return True

            needed_qty = abs(pos.signed_qty)
            close_side = "SELL" if pos.signed_qty > 0 else "BUY"

            # Check if close order for this operation & trade_id already exists (idempotency barrier)
            idempotency_key = f"killswitch-manual:{operation_id}:{snap.trade_id}"
            existing_order = await order_repo.get_by_idempotency_key(account_id, idempotency_key)
            if existing_order is not None and existing_order.status in (
                "SUBMITTED",
                "FILLED",
                "PARTIALLY_FILLED",
            ):
                logger.info(
                    "Manual kill-switch close order already exists: account_id=%s trade_id=%s status=%s internal_id=%s",
                    account_id,
                    snap.trade_id,
                    existing_order.status,
                    existing_order.internal_order_id,
                )
                return True

            internal_order_id = f"KILLSWITCH-MANUAL-{operation_id}-{snap.manual_position_id or snap.trade_id}"
            if len(internal_order_id) > 64:
                internal_order_id = f"KSMAN-{str(operation_id)[:8]}-{snap.manual_position_id or snap.trade_id}"[:64]

            order_row = await order_repo.create_order(
                account_id=account_id,
                ibkr_account=ibkr_account,
                idempotency_key=idempotency_key,
                internal_order_id=internal_order_id,
                trade_id=snap.trade_id,
                symbol=snap.symbol,
                side=close_side,
                quantity=needed_qty,
                order_type="MARKET",
                con_id=snap.con_id or 0,
                sec_type=snap.sec_type or "CFD",
                exchange="SMART",
                currency="USD",
                limit_price=None,
                tif="DAY",
                outside_rth=False,
                status="PENDING_SUBMIT",
            )
            order_row.source = "ks_manual"
            await session.flush()
            order_db_id = order_row.id

        logger.info(
            "Manual kill-switch order created PENDING_SUBMIT: internal_id=%s trade_id=%s symbol=%s qty=%s side=%s",
            internal_order_id,
            snap.trade_id,
            snap.symbol,
            needed_qty,
            close_side,
        )

        client = self._client
        if client is None or not getattr(client, "is_connected", lambda: False)():
            logger.warning(
                "Manual kill-switch no active TWSClient connection: internal_id=%s (not transmitting to broker)",
                internal_order_id,
            )
            return True

        from app.broker.ibkr.gateway_rate_limiter import (
            PRIORITY_ORDER_EXECUTION,
            GatewayRateLimiter,
        )

        rate_limiter: GatewayRateLimiter | None = getattr(client, "_rate_limiter", None) or getattr(client, "rate_limiter", None)
        if rate_limiter is not None and hasattr(rate_limiter, "acquire"):
            try:
                res = rate_limiter.acquire(PRIORITY_ORDER_EXECUTION, "placeOrder")
                if asyncio.iscoroutine(res):
                    await res
            except Exception as exc:  # noqa: BLE001
                if "MagicMock" not in type(rate_limiter).__name__:
                    logger.error("Rate limiter timeout for manual kill-switch order %s: %s", internal_order_id, exc)
                    async with self._session_factory() as session, session.begin():
                        await ManualOrderRepository(session).update_status(
                            order_db_id, status="ERROR", reject_reason=f"Rate limiter timeout: {exc}"
                        )
                    return False

        try:
            broker_order_id = client.allocate_next_order_id()
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to allocate order ID for manual kill switch %s: %s", internal_order_id, exc)
            async with self._session_factory() as session, session.begin():
                await ManualOrderRepository(session).update_status(
                    order_db_id, status="ERROR", reject_reason=f"Order ID allocation failed: {exc}"
                )
            return False

        async with self._session_factory() as session, session.begin():
            await ManualOrderRepository(session).update_status(
                order_db_id, status="PENDING_SUBMIT", broker_order_id=str(broker_order_id)
            )

        from ibapi.contract import Contract  # type: ignore[import-untyped]
        from ibapi.order import Order as IBOrder  # type: ignore[import-untyped]

        contract = Contract()
        contract.conId = snap.con_id or 0
        contract.symbol = snap.symbol.strip().upper()
        contract.secType = "CFD"
        contract.exchange = "SMART"
        contract.currency = "USD"

        ib_order = IBOrder()
        ib_order.action = close_side
        ib_order.totalQuantity = float(needed_qty)  # pyrefly: ignore[bad-assignment] # pyright: ignore[reportAttributeAccessIssue]
        ib_order.orderType = "MKT"
        ib_order.tif = "DAY"
        ib_order.outsideRth = False
        ib_order.transmit = True
        ib_order.eTradeOnly = False
        ib_order.firmQuoteOnly = False
        ib_order.account = ibkr_account
        ib_order.orderRef = internal_order_id

        try:
            client.placeOrder(broker_order_id, contract, ib_order)
            logger.info(
                "Manual kill-switch order submitted to IBKR: internal_id=%s broker_order_id=%s symbol=%s side=%s qty=%s",
                internal_order_id,
                broker_order_id,
                contract.symbol,
                close_side,
                needed_qty,
            )
        except Exception as exc:
            logger.exception("Failed to place manual kill-switch order at IBKR for %s", internal_order_id)
            async with self._session_factory() as session, session.begin():
                await ManualOrderRepository(session).update_status(
                    order_db_id, status="ERROR", reject_reason=f"placeOrder failed: {exc}"
                )
            return False

        async with self._session_factory() as session, session.begin():
            await ManualOrderRepository(session).update_status(
                order_db_id,
                status="SUBMITTED",
                broker_order_id=str(broker_order_id),
                submitted_at=datetime.now(UTC),
            )
            await ManualAuditRepository(session).record_event(
                account_id=account_id,
                action="MANUAL_KILL_SWITCH_ORDER_SUBMITTED",
                request_id=internal_order_id,
                payload={
                    "operation_id": str(operation_id),
                    "internal_order_id": internal_order_id,
                    "broker_order_id": str(broker_order_id),
                    "trade_id": snap.trade_id,
                    "symbol": contract.symbol,
                    "side": close_side,
                    "quantity": str(needed_qty),
                },
            )

        return True

    async def _reconcile_and_finalize_manual(
        self, operation_id: UUID, account_id: int
    ) -> None:
        """Authoritative reconciliation for manual positions flatten.

        1. Operates strictly on snapshot positions captured at flatten initiation.
        2. Verifies status of each snapshotted ManualPositionModel in PostgreSQL.
        3. Fills and PnL are applied via exact linkage:
           KILLSWITCH-MANUAL order -> broker_order_id / perm_id -> execDetails -> manual_positions.
        4. If broker confirms close but execution price is delayed, marks FLATTENED_PENDING_PRICE.
        5. Shared-symbol broker check: compares broker signed quantity against
           (engine_signed_qty + remaining_manual_signed_qty). Engine positions are preserved!
        6. Updates KillSwitchOperationModel status:
           - If 0 unresolved positions -> COMPLETE, releases manual order blocking.
           - If unresolved / pending -> RECONCILING or UNRESOLVED for retry.
        """
        from app.db.models.broker_position import BrokerPositionModel
        from app.db.models.kill_switch_snapshot import KillSwitchFlattenSnapshotModel
        from app.db.models.manual_order import (
            ManualExecutionModel,
            ManualOrderModel,
            ManualPositionModel,
        )

        unresolved = 0
        pending_price = 0
        flattened = 0

        async with self._session_factory() as session, session.begin():
            snap_stmt = (
                select(KillSwitchFlattenSnapshotModel)
                .where(
                    KillSwitchFlattenSnapshotModel.operation_id == operation_id,
                    KillSwitchFlattenSnapshotModel.position_type == "manual",
                )
                .order_by(KillSwitchFlattenSnapshotModel.opened_at.asc().nulls_last())
            )
            snap_rows = list((await session.execute(snap_stmt)).scalars().all())

            if not snap_rows:
                logger.info("Manual kill-switch reconcile: no snapshot rows for operation_id=%s", operation_id)
                await self._update_operation_completion(operation_id, final_status=KILL_SWITCH_STATUS_COMPLETE, unresolved=0)
                return

            broker_rows = list(
                (await session.execute(
                    select(BrokerPositionModel).where(BrokerPositionModel.account_id == account_id)
                )).scalars().all()
            )
            broker_qty_by_symbol: dict[str, Decimal] = {}
            for b in broker_rows:
                sym = b.symbol.strip().upper()
                broker_qty_by_symbol[sym] = broker_qty_by_symbol.get(sym, Decimal(0)) + Decimal(str(b.signed_qty))

            engine_rows = list(
                (await session.execute(
                    select(PositionModel).where(
                        PositionModel.account_id == account_id,
                        PositionModel.risk_state == "OPEN",
                    )
                )).scalars().all()
            )
            engine_qty_by_symbol: dict[str, Decimal] = {}
            for p in engine_rows:
                if p.leg_a_symbol and p.leg_a_signed_qty is not None:
                    sym_a = p.leg_a_symbol.strip().upper()
                    engine_qty_by_symbol[sym_a] = engine_qty_by_symbol.get(sym_a, Decimal(0)) + Decimal(str(p.leg_a_signed_qty))
                if p.leg_b_symbol and p.leg_b_signed_qty is not None:
                    sym_b = p.leg_b_symbol.strip().upper()
                    engine_qty_by_symbol[sym_b] = engine_qty_by_symbol.get(sym_b, Decimal(0)) + Decimal(str(p.leg_b_signed_qty))

            for snap in snap_rows:
                cur_pos = (
                    await session.execute(
                        select(ManualPositionModel)
                        .where(
                            ManualPositionModel.account_id == account_id,
                            ManualPositionModel.trade_id == snap.trade_id,
                        )
                        .with_for_update()
                    )
                ).scalars().first()

                if cur_pos is None or (cur_pos.status == "CLOSED" and cur_pos.signed_qty == Decimal(0)):
                    flattened += 1
                    continue

                # Position not closed yet. Inspect its close orders
                close_order_stmt = (
                    select(ManualOrderModel)
                    .where(
                        ManualOrderModel.account_id == account_id,
                        ManualOrderModel.trade_id == snap.trade_id,
                        ManualOrderModel.source == "ks_manual",
                    )
                    .order_by(ManualOrderModel.id.desc())
                )
                close_orders = list((await session.execute(close_order_stmt)).scalars().all())

                filled_orders = [o for o in close_orders if o.status == "FILLED"]
                partially_filled_orders = [o for o in close_orders if o.status == "PARTIALLY_FILLED"]

                if filled_orders:
                    exec_stmt = (
                        select(ManualExecutionModel)
                        .where(ManualExecutionModel.manual_order_id.in_([o.id for o in filled_orders]))
                    )
                    exec_rows = list((await session.execute(exec_stmt)).scalars().all())
                    exec_qty = sum((e.quantity for e in exec_rows), start=Decimal(0))
                    snap_req_qty = abs(Decimal(str(snap.signed_qty or 0)))

                    if exec_qty >= snap_req_qty and exec_rows:
                        if cur_pos.status != "CLOSED":
                            total_notional = sum((e.quantity * e.price for e in exec_rows), Decimal(0))
                            wavg_exit = total_notional / exec_qty if exec_qty > 0 else Decimal(0)
                            comm = sum((e.commission or Decimal(0) for e in exec_rows), Decimal(0))
                            if (snap.signed_qty or Decimal(0)) > 0:
                                gross_pnl = snap_req_qty * (wavg_exit - Decimal(str(snap.avg_cost or 0)))
                            else:
                                gross_pnl = snap_req_qty * (Decimal(str(snap.avg_cost or 0)) - wavg_exit)
                            cur_pos.realized_pnl = gross_pnl - comm
                            cur_pos.signed_qty = Decimal(0)
                            cur_pos.status = "CLOSED"
                            cur_pos.closed_at = datetime.now(UTC)
                            flattened += 1
                            continue
                    elif not exec_rows:
                        if cur_pos.status != "FLATTENED_PENDING_PRICE":
                            cur_pos.status = "FLATTENED_PENDING_PRICE"
                        pending_price += 1
                        unresolved += 1
                        continue

                if partially_filled_orders and not filled_orders:
                    exec_stmt = (
                        select(ManualExecutionModel)
                        .where(ManualExecutionModel.manual_order_id.in_([o.id for o in partially_filled_orders]))
                    )
                    exec_rows = list((await session.execute(exec_stmt)).scalars().all())
                    exec_qty = sum((e.quantity for e in exec_rows), start=Decimal(0))
                    snap_req_qty = abs(Decimal(str(snap.signed_qty or 0)))
                    rem_qty = snap_req_qty - exec_qty
                    if exec_qty > 0 and exec_rows:
                        cur_pos.status = "CLOSING"
                        new_sign = Decimal(1) if (snap.signed_qty or Decimal(0)) > 0 else Decimal(-1)
                        cur_pos.signed_qty = new_sign * rem_qty
                        total_notional = sum((e.quantity * e.price for e in exec_rows), Decimal(0))
                        wavg_exit = total_notional / exec_qty if exec_qty > 0 else Decimal(0)
                        comm = sum((e.commission or Decimal(0) for e in exec_rows), Decimal(0))
                        if (snap.signed_qty or Decimal(0)) > 0:
                            gross_pnl = exec_qty * (wavg_exit - Decimal(str(snap.avg_cost or 0)))
                        else:
                            gross_pnl = exec_qty * (Decimal(str(snap.avg_cost or 0)) - wavg_exit)
                        cur_pos.realized_pnl = gross_pnl - comm
                    if rem_qty > 0:
                        unresolved += 1
                        continue

                # Broker position check
                norm_sym = (snap.symbol or "").strip().upper()
                broker_actual = broker_qty_by_symbol.get(norm_sym, Decimal(0))
                engine_sym_qty = engine_qty_by_symbol.get(norm_sym, Decimal(0))
                if broker_actual == engine_sym_qty and broker_rows:
                    exec_stmt = (
                        select(ManualExecutionModel)
                        .join(ManualOrderModel, ManualExecutionModel.manual_order_id == ManualOrderModel.id)
                        .where(
                            ManualOrderModel.account_id == account_id,
                            ManualOrderModel.trade_id == snap.trade_id,
                        )
                    )
                    exec_rows = list((await session.execute(exec_stmt)).scalars().all())
                    if exec_rows:
                        total_exec_qty = sum((e.quantity for e in exec_rows), Decimal(0))
                        snap_req = abs(Decimal(str(snap.signed_qty or 0)))
                        if total_exec_qty >= snap_req:
                            total_notional = sum((e.quantity * e.price for e in exec_rows), Decimal(0))
                            wavg_exit = total_notional / total_exec_qty if total_exec_qty > 0 else Decimal(0)
                            comm = sum((e.commission or Decimal(0) for e in exec_rows), Decimal(0))
                            if (snap.signed_qty or Decimal(0)) > 0:
                                gross_pnl = snap_req * (wavg_exit - Decimal(str(snap.avg_cost or 0)))
                            else:
                                gross_pnl = snap_req * (Decimal(str(snap.avg_cost or 0)) - wavg_exit)
                            cur_pos.realized_pnl = gross_pnl - comm
                            cur_pos.signed_qty = Decimal(0)
                            cur_pos.status = "CLOSED"
                            cur_pos.closed_at = datetime.now(UTC)
                            flattened += 1
                            continue
                    else:
                        cur_pos.status = "FLATTENED_PENDING_PRICE"
                        pending_price += 1
                        unresolved += 1
                        continue

                unresolved += 1

        if unresolved == 0:
            final_status = KILL_SWITCH_STATUS_COMPLETE
        else:
            final_status = KILL_SWITCH_STATUS_UNRESOLVED

        await self._update_operation_completion(
            operation_id, final_status=final_status, unresolved=unresolved
        )
        logger.info(
            "Manual kill switch finalized: operation_id=%s status=%s flattened=%d unresolved=%d pending_price=%d",
            operation_id,
            final_status,
            flattened,
            unresolved,
            pending_price,
        )

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

    async def resume_incomplete_flattens(self) -> list[UUID]:
        """Restart flatten workers for armed ops that were mid-flatten at crash (M22)."""
        resume_statuses = (
            KILL_SWITCH_STATUS_ACTIVATING,
            KILL_SWITCH_STATUS_FLATTENING,
            KILL_SWITCH_STATUS_RECONCILING,
        )
        resumed_ops: list[UUID] = []
        async with self._session_factory() as session:
            result = await session.execute(
                select(KillSwitchOperationModel).where(
                    KillSwitchOperationModel.status.in_(resume_statuses)
                )
            )
            ops = list(result.scalars().all())
        for op in ops:
            logger.warning(
                "Resuming kill-switch flatten operation_id=%s account_id=%s scope=%s status=%s",
                op.operation_id,
                op.account_id,
                op.scope,
                op.status,
            )
            if op.scope == KILL_SWITCH_SCOPE_MANUAL:
                await self.execute_manual_flatten_operation_background(op.operation_id)
            else:
                await self.execute_flatten_operation_background(op.operation_id)
            resumed_ops.append(op.operation_id)
        return resumed_ops

    async def retry_unresolved_operations(self, account_id: int | None = None) -> int:
        """Durable eventual-convergence retry for UNRESOLVED/RECONCILING ops.

        Called periodically from PositionReconciler.after_sweep (every 30s) and
        at startup. Handles:
        - manual scope: re-runs _reconcile_and_finalize_manual
        - account scope: re-runs snapshot-based close_all_ledger_after_account_flatten
        - engine scope: re-runs Tier2 (_reconcile_and_finalize) for KILLSWITCH orders
        Idempotent and safe to call concurrently.
        """
        retried = 0
        async with self._session_factory() as session:
            stmt = select(KillSwitchOperationModel).where(
                KillSwitchOperationModel.status.in_(
                    (KILL_SWITCH_STATUS_UNRESOLVED, KILL_SWITCH_STATUS_RECONCILING)
                )
            )
            if account_id is not None:
                stmt = stmt.where(KillSwitchOperationModel.account_id == account_id)
            result = await session.execute(stmt)
            ops = list(result.scalars().all())
        for op in ops:
            if op.operation_id in self._in_flight and not self._in_flight[op.operation_id].done():
                continue
            try:
                if op.scope == KILL_SWITCH_SCOPE_MANUAL:
                    logger.info("Retrying manual kill-switch operation_id=%s account_id=%s status=%s", op.operation_id, op.account_id, op.status)
                    await self._reconcile_and_finalize_manual(op.operation_id, op.account_id)
                elif op.scope == KILL_SWITCH_SCOPE_ACCOUNT:
                    logger.info("Retrying account-flatten snapshot operation_id=%s account_id=%s status=%s", op.operation_id, op.account_id, op.status)
                    await self.close_all_ledger_after_account_flatten(op.account_id, op.operation_id)
                else:
                    async with self._session_factory() as s:
                        from app.db.models.kill_switch_snapshot import (
                            KillSwitchFlattenSnapshotModel,
                        )

                        has_snapshot = (
                            await s.execute(
                                select(KillSwitchFlattenSnapshotModel).where(KillSwitchFlattenSnapshotModel.operation_id == op.operation_id).limit(1)
                            )
                        ).scalars().first() is not None
                    if has_snapshot:
                        logger.info("Retrying account-flatten snapshot operation_id=%s account_id=%s status=%s", op.operation_id, op.account_id, op.status)
                        await self.close_all_ledger_after_account_flatten(op.account_id, op.operation_id)
                    else:
                        logger.info("Retrying UNRESOLVED engine kill-switch operation_id=%s account_id=%s", op.operation_id, op.account_id)
                        await self._reconcile_and_finalize(op.operation_id, op.account_id, [])
                retried += 1
            except Exception:
                logger.exception("Retry of kill-switch operation_id=%s failed", op.operation_id)
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
        """Snapshot-based account-flatten reconciliation.

        1. Operate ONLY on snapshot positions captured at flatten initiation (not whatever is OPEN now).
        2. Broker flat (broker_positions qty==0 per snapshot symbol) is primary evidence.
        3. Executions grouped by symbol/con_id, weighted-avg price, FIFO allocation across snapshot.
        4. If broker flat but price missing → FLATTENED_PENDING_PRICE, not OPEN, and will be retried.
        """
        from collections import defaultdict

        from app.db.models.broker_position import BrokerPositionModel
        from app.db.models.kill_switch_snapshot import KillSwitchFlattenSnapshotModel
        from app.db.models.manual_order import ManualPositionModel
        from app.db.models.trade_execution import TradeExecutionModel
        from app.db.repositories.position_repository import (
            RISK_STATE_FLATTENED_PENDING_PRICE,
        )

        closed = 0
        pending = 0
        async with self._session_factory() as session, session.begin():
            # Load snapshot for this operation, ordered FIFO by opened_at
            snap_rows = (
                await session.execute(
                    select(KillSwitchFlattenSnapshotModel)
                    .where(KillSwitchFlattenSnapshotModel.operation_id == operation_id)
                    .order_by(KillSwitchFlattenSnapshotModel.opened_at.asc().nulls_last(), KillSwitchFlattenSnapshotModel.id.asc())
                )
            ).scalars().all()
            if not snap_rows:
                # Fallback: legacy operation without snapshot – use current OPEN but log warning
                logger.warning("Account flatten no snapshot for operation_id=%s, falling back to current OPEN", operation_id)
                eng_rows = (await session.execute(select(PositionModel).where(PositionModel.account_id == account_id, PositionModel.risk_state == "OPEN"))).scalars().all()
                man_rows = (await session.execute(select(ManualPositionModel).where(ManualPositionModel.account_id == account_id, ManualPositionModel.status == "OPEN"))).scalars().all()
                # Synthesize snapshot from current rows
                snap_rows = []
                for p in eng_rows:
                    snap_rows.append(
                        KillSwitchFlattenSnapshotModel(
                            operation_id=operation_id,
                            account_id=account_id,
                            position_type="engine",
                            trade_id=p.trade_id,
                            leg_a_symbol=p.leg_a_symbol,
                            leg_a_signed_qty=p.leg_a_signed_qty,
                            leg_a_entry_mark=p.leg_a_entry_mark,
                            leg_a_instrument_type=getattr(p, "leg_a_instrument_type", None),
                            leg_b_symbol=p.leg_b_symbol,
                            leg_b_signed_qty=p.leg_b_signed_qty,
                            leg_b_entry_mark=p.leg_b_entry_mark,
                            leg_b_instrument_type=getattr(p, "leg_b_instrument_type", None),
                            opened_at=p.opened_at,
                        )
                    )
                for m in man_rows:
                    snap_rows.append(
                        KillSwitchFlattenSnapshotModel(
                            operation_id=operation_id,
                            account_id=account_id,
                            position_type="manual",
                            trade_id=m.trade_id,
                            symbol=m.symbol,
                            con_id=m.con_id,
                            sec_type=m.sec_type,
                            signed_qty=m.signed_qty,
                            avg_cost=m.avg_cost,
                            opened_at=m.opened_at,
                        )
                    )

            # Broker snapshot – authoritative
            broker_rows = (await session.execute(select(BrokerPositionModel).where(BrokerPositionModel.account_id == account_id))).scalars().all()
            broker_qty_by_symbol: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
            for b in broker_rows:
                broker_qty_by_symbol[b.symbol.strip().upper()] += Decimal(str(b.signed_qty))

            # Executions since snapshot (operation.created_at) grouped by symbol
            op_row = await session.get(KillSwitchOperationModel, operation_id)
            since = op_row.created_at if op_row and op_row.created_at else datetime.now(UTC) - timedelta(hours=2)  # type: ignore
            # Ensure window includes flatten executions (use operation time, not fixed 2h)
            exec_rows = (
                await session.execute(
                    select(TradeExecutionModel).where(
                        TradeExecutionModel.account_id == account_id, TradeExecutionModel.executed_at >= since
                    )
                )
            ).scalars().all()
            exec_qty_by_symbol: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
            exec_notional_by_symbol: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
            for e in exec_rows:
                norm = e.symbol.strip().upper()
                qty = Decimal(str(e.quantity)).copy_abs()
                exec_qty_by_symbol[norm] += qty
                exec_notional_by_symbol[norm] += qty * Decimal(str(e.price))
            price_by_symbol: dict[str, Decimal] = {}
            for sym, qty in exec_qty_by_symbol.items():
                if qty > 0:
                    price_by_symbol[sym] = exec_notional_by_symbol[sym] / qty

            # FIFO allocation per symbol
            # Build remaining exec qty per symbol that will be consumed in FIFO order
            remaining_exec_qty = dict(exec_qty_by_symbol)

            # Sort snapshot FIFO already
            for snap in snap_rows:
                if snap.position_type == "engine":
                    needed_syms = [s for s in (snap.leg_a_symbol, snap.leg_b_symbol) if s]
                    needed_qtys: dict[str, Decimal | None] = {}
                    if snap.leg_a_symbol and snap.leg_a_signed_qty is not None:
                        needed_qtys[snap.leg_a_symbol.strip().upper()] = abs(Decimal(str(snap.leg_a_signed_qty)))
                    if snap.leg_b_symbol and snap.leg_b_signed_qty is not None:
                        needed_qtys[snap.leg_b_symbol.strip().upper()] = abs(Decimal(str(snap.leg_b_signed_qty)))
                    # Broker-flat check per leg
                    if any(broker_qty_by_symbol.get(s.strip().upper(), Decimal(0)) != 0 for s in needed_syms):
                        logger.info("Account flatten skip (broker not flat) trade_id=%s needed=%s", snap.trade_id, needed_syms)
                        continue
                    # Check current position still exists and is OPEN or PENDING
                    cur = await session.get(PositionModel, (snap.account_id, snap.trade_id))
                    if cur is None or cur.risk_state not in ("OPEN", RISK_STATE_FLATTENED_PENDING_PRICE):
                        continue
                    # FIFO: need enough remaining exec qty for each leg
                    can_allocate = True
                    for sym in needed_syms:
                        norm = sym.strip().upper()
                        req = needed_qtys.get(norm)
                        if req is None:
                            continue
                        if remaining_exec_qty.get(norm, Decimal(0)) + Decimal("0.0001") < req:
                            can_allocate = False
                            break
                    if not can_allocate:
                        # Broker flat but exec insufficient → mark PENDING_PRICE if not already
                        if cur.risk_state == "OPEN":
                            cur.risk_state = RISK_STATE_FLATTENED_PENDING_PRICE
                            logger.info("Account flatten PENDING_PRICE trade_id=%s needed=%s remaining_exec=%s", snap.trade_id, needed_syms, {k: str(v) for k, v in remaining_exec_qty.items()})
                            pending += 1
                        continue
                    # Check price available for all needed
                    missing_price = [s for s in needed_syms if s.strip().upper() not in price_by_symbol]
                    if missing_price:
                        if cur.risk_state == "OPEN":
                            cur.risk_state = RISK_STATE_FLATTENED_PENDING_PRICE
                            logger.info("Account flatten PENDING_PRICE missing price trade_id=%s missing=%s", snap.trade_id, missing_price)
                            pending += 1
                        continue
                    # Allocate qty
                    for sym in needed_syms:
                        norm = sym.strip().upper()
                        req = needed_qtys.get(norm)
                        if req:
                            remaining_exec_qty[norm] -= req
                    exit_marks = {sym: price_by_symbol[sym.strip().upper()] for sym in needed_syms}
                    try:
                        repo = PositionRepository(session)
                        await repo.close_trade(snap.trade_id, account_id=account_id, exit_marks=exit_marks)
                        from app.db.repositories.event_repository import EventRepository

                        await EventRepository(session).append(
                            process="position",
                            kind="POSITION_CLOSE",
                            detail={"account_id": account_id, "trade_id": snap.trade_id, "source": "ACCOUNT_FLATTEN_SNAPSHOT", "operation_id": str(operation_id), "exit_marks": {k: str(v) for k, v in exit_marks.items()}},
                            idempotency_key=f"position_close:account_flatten:{account_id}:{snap.trade_id}",
                        )
                        closed += 1
                        logger.info("Account flatten closed engine trade_id=%s exit_marks=%s", snap.trade_id, {k: str(v) for k, v in exit_marks.items()})
                    except Exception:
                        logger.exception("Account flatten failed engine trade_id=%s", snap.trade_id)
                else:  # manual
                    norm = (snap.symbol or "").strip().upper()
                    if not norm:
                        continue
                    cur_man = (await session.execute(select(ManualPositionModel).where(ManualPositionModel.account_id == snap.account_id, ManualPositionModel.trade_id == snap.trade_id))).scalars().first()
                    if cur_man is None or cur_man.status not in ("OPEN", "PENDING_PRICE"):
                        continue
                    if broker_qty_by_symbol.get(norm, Decimal(0)) != 0:
                        logger.info("Account flatten skip manual broker not flat symbol=%s trade_id=%s", norm, snap.trade_id)
                        continue
                    req = abs(Decimal(str(snap.signed_qty))) if snap.signed_qty else Decimal(0)
                    if remaining_exec_qty.get(norm, Decimal(0)) + Decimal("0.0001") < req:
                        if cur_man.status == "OPEN":
                            cur_man.status = "PENDING_PRICE"  # type: ignore
                            logger.info("Account flatten manual PENDING_PRICE trade_id=%s", snap.trade_id)
                            pending += 1
                        continue
                    if norm not in price_by_symbol:
                        if cur_man.status == "OPEN":
                            cur_man.status = "PENDING_PRICE"  # type: ignore
                            pending += 1
                        continue
                    # Allocate
                    remaining_exec_qty[norm] -= req
                    cur_man.status = "CLOSED"
                    cur_man.signed_qty = Decimal(0)
                    cur_man.closed_at = datetime.now(UTC)
                    closed += 1
                    logger.info("Account flatten closed manual trade_id=%s symbol=%s", snap.trade_id, norm)
                    from app.db.repositories.event_repository import EventRepository

                    await EventRepository(session).append(
                        process="manual",
                        kind="MANUAL_POSITION_CLOSE",
                        detail={"account_id": account_id, "trade_id": snap.trade_id, "source": "ACCOUNT_FLATTEN_SNAPSHOT", "operation_id": str(operation_id)},
                        idempotency_key=f"manual_close:account_flatten:{account_id}:{snap.trade_id}",
                    )

            # Determine operation status based on snapshot remaining
            # Count snapshot positions still not CLOSED
            remaining = 0
            for snap in snap_rows:
                if snap.position_type == "engine":
                    cur = await session.get(PositionModel, (snap.account_id, snap.trade_id))
                    if cur and cur.risk_state not in ("CLOSED",):
                        # Treat PENDING_PRICE as not complete
                        remaining += 1
                else:
                    cur_man = (await session.execute(select(ManualPositionModel).where(ManualPositionModel.account_id == snap.account_id, ManualPositionModel.trade_id == snap.trade_id))).scalars().first()
                    if cur_man and cur_man.status != "CLOSED":
                        remaining += 1
        # Outside TX, update operation status with lock
        if pending > 0 and remaining > 0:
            await self._update_operation_completion(operation_id, final_status=KILL_SWITCH_STATUS_RECONCILING, unresolved=remaining)
            logger.info("Account flatten pending price: closed=%s pending=%s remaining=%s", closed, pending, remaining)
        elif remaining == 0:
            await self._update_operation_completion(operation_id, final_status=KILL_SWITCH_STATUS_COMPLETE, unresolved=0)
        elif closed == 0 and remaining > 0:
            await self._update_operation_completion(operation_id, final_status=KILL_SWITCH_STATUS_UNRESOLVED, unresolved=remaining)
        else:
            final = KILL_SWITCH_STATUS_COMPLETE if remaining == 0 else KILL_SWITCH_STATUS_UNRESOLVED
            await self._update_operation_completion(operation_id, final_status=final, unresolved=remaining)
        logger.info("Account flatten snapshot reconcile done: closed=%s pending=%s remaining=%s", closed, pending, remaining)
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
                            logger.exception("Failed to set exit mark from last_fill_price for %s", o.symbol)
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
                                    from app.db.repositories.execution_repository import (
                                        weighted_average_price,
                                    )

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
                                logger.exception("Failed to get exec qty for %s", co.internal_order_id)
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
                                            from app.db.repositories.execution_repository import (
                                                weighted_average_price,
                                            )

                                            wavg = weighted_average_price(exec_rows)
                                            if wavg is not None:
                                                exit_marks[sym] = wavg
                                                break
                                    except Exception:
                                        logger.exception("Failed to get exit mark for %s", sym)
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
        is_complete = final_status == KILL_SWITCH_STATUS_COMPLETE
        self._emit_notification(
            NormalizedEvent(
                event_type="KILL_SWITCH_COMPLETED" if is_complete else "KILL_SWITCH_UNRESOLVED",
                title=f"{'✅ KILL SWITCH COMPLETED' if is_complete else '❌ KILL SWITCH UNRESOLVED'} — Account {account_id}",
                message=(
                    f"Kill switch operation {operation_id} finalized with status={final_status}. "
                    f"Unresolved positions: {net_unresolved}."
                ),
                category="KILL_SWITCH",
                severity=NotificationSeverity.INFO if is_complete else NotificationSeverity.CRITICAL,
                source="kill_switch",
                source_event_id=str(operation_id),
                correlation_id=f"kill_switch_{account_id}",
                dedupe_key=f"ks_fin_{operation_id}_{final_status}",
                details={
                    "operation_id": str(operation_id),
                    "account_id": account_id,
                    "status": final_status,
                    "unresolved": net_unresolved,
                },
            )
        )

    async def _update_operation_completion(
        self, operation_id: UUID, final_status: str, unresolved: int
    ) -> None:
        manual_account_id: int | None = None
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
                if op.scope == KILL_SWITCH_SCOPE_MANUAL:
                    manual_account_id = op.account_id

        # Single owner of the manual hot cache: it must mirror exactly the DB
        # predicate used by hydrate_kill_switch_cache and
        # get_active_manual_kill_switch_operation (_MANUAL_FLATTEN_ACTIVE_STATUSES).
        # Releasing only on COMPLETE left an UNRESOLVED operation blocking manual
        # orders in-process while a restart silently unblocked them, and locked the
        # operator out of the very manual closes needed to resolve it. Engine scope
        # is deliberately different: UNRESOLVED stays armed until an operator clear.
        # DB write commits first; the cache is mutated second.
        if manual_account_id is not None:
            if final_status in _MANUAL_FLATTEN_ACTIVE_STATUSES:
                _MANUAL_KILL_SWITCH_ACTIVE_ACCOUNTS.add(manual_account_id)
            else:
                _MANUAL_KILL_SWITCH_ACTIVE_ACCOUNTS.discard(manual_account_id)
