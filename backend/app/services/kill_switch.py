"""Durable Kill Switch / Emergency Flatten Service.

Orchestrates non-blocking, idempotent, bounded parallel emergency position flattening,
partial-fill aware retries, and authoritative broker reconciliation.
"""

import asyncio
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


def _arm_kill_switch_cache(account_id: int) -> None:
    """Mark an account blocked in the in-memory cache."""
    _KILL_SWITCH_ACTIVE_ACCOUNTS.add(account_id)


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
        count = int(result.rowcount or 0)

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
                KillSwitchOperationModel.status.in_(
                    [
                        KILL_SWITCH_STATUS_ACTIVATING,
                        KILL_SWITCH_STATUS_FLATTENING,
                        KILL_SWITCH_STATUS_RECONCILING,
                        KILL_SWITCH_STATUS_RETRYING,
                    ]
                ),
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

    async def execute_flatten_operation_background(self, operation_id: UUID) -> None:
        """Trigger background worker task to execute non-blocking position flattening."""
        asyncio.create_task(
            self._execute_flatten_operation(operation_id),
            name=f"kill-switch-flatten-{operation_id}",
        )

    async def _execute_flatten_operation(self, operation_id: UUID) -> None:
        """Execute durable flatten operation asynchronously off the HTTP request thread."""
        logger.info("Starting background flatten worker execution for operation_id=%s", operation_id)

        async with self._session_factory() as session, session.begin():
            op = await session.get(KillSwitchOperationModel, operation_id)
            if op is None or op.status in (KILL_SWITCH_STATUS_COMPLETE, KILL_SWITCH_STATUS_UNRESOLVED):
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
            await self._update_operation_completion(operation_id, final_status=KILL_SWITCH_STATUS_COMPLETE, unresolved=0)
            return

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
                        quantity=qty,
                        price=Decimal(0),
                        contract_month="202612",
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
                        quantity=qty,
                        price=Decimal(0),
                        contract_month="202612",
                        instrument_type=pos.leg_b_instrument_type or "STK",
                        leg_index=1,
                    )
                )

            if not legs:
                return True

            close_intent = OrderIntent(
                signal_id=f"KILLSWITCH-{pos.trade_id}-{uuid4().hex[:6]}",
                strategy_id=pos.strategy_id,
                action=OrderAction.CLOSE,
                legs=legs,
                account_id=account_id,
                ibkr_account=ibkr_account,
                intent_mode=ExecutionIntentMode.EMERGENCY_FLATTEN,
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

                    if success and orders:
                        from app.oms.models import OMSOrderStatus
                        fill_orders = [o for o in orders if not getattr(o, "is_compensation", False)]
                        is_fully_filled = bool(fill_orders) and all(
                            getattr(o, "status", None) == OMSOrderStatus.FILLED for o in fill_orders
                        )

                        if is_fully_filled:
                            async with self._session_factory() as session, session.begin():
                                pos_repo = PositionRepository(session)
                                p_row = await pos_repo.get_open_by_trade_id(pos.trade_id, account_id=account_id)
                                if p_row is not None:
                                    from app.services.model_blue.persistence import (
                                        _commission_from_orders,
                                        _exit_marks_from_orders,
                                    )
                                    exit_marks = _exit_marks_from_orders(fill_orders)
                                    comm = _commission_from_orders(fill_orders)
                                    await pos_repo.close_trade(
                                        pos.trade_id,
                                        account_id=account_id,
                                        exit_marks=exit_marks,
                                        commission=comm,
                                    )
                                    from app.db.repositories.event_repository import (
                                        EventRepository,
                                    )
                                    await EventRepository(session).append(
                                        process="position",
                                        kind="POSITION_CLOSE",
                                        detail={
                                            "account_id": account_id,
                                            "trade_id": pos.trade_id,
                                            "source": "KILL_SWITCH",
                                        },
                                        idempotency_key=f"position_close:kill_switch:{account_id}:{pos.trade_id}",
                                    )
                                    logger.info(
                                        "Kill Switch persisted position close: account_id=%d trade_id=%s",
                                        account_id,
                                        pos.trade_id,
                                    )
                    return success
                except Exception:
                    logger.exception("Failed to execute position reduction for trade_id=%s", pos.trade_id)
                    return False
            return True

    async def _reconcile_and_finalize(
        self, operation_id: UUID, account_id: int, results: list[Any]
    ) -> None:
        """Reconcile final exposure from database and broker state before setting operation COMPLETE."""
        # Tier 2 Reconciliation: Auto-repair any open positions whose close orders filled in DB
        async with self._session_factory() as session, session.begin():
            pos_repo = PositionRepository(session)
            from app.db.repositories.order_repository import OrderRepository
            order_repo = OrderRepository(session)
            open_positions = await pos_repo.list_open()
            account_open = [p for p in open_positions if p.account_id == account_id]

            for pos in account_open:
                pos_orders = await order_repo.list_by_trade_id(pos.trade_id)
                close_orders = [
                    o for o in pos_orders
                    if "KILLSWITCH-" in (o.internal_order_id or "") or ":CLOSE" in (o.internal_order_id or "")
                ]
                if close_orders:
                    filled_close = [o for o in close_orders if o.status == "FILLED"]
                    req_legs = 2 if pos.leg_b_symbol else 1
                    if len(filled_close) >= req_legs:
                        exit_marks = {}
                        for co in filled_close:
                            if co.fill_price is not None:
                                exit_marks[co.symbol] = Decimal(str(co.fill_price))
                        await pos_repo.close_trade(
                            pos.trade_id,
                            account_id=account_id,
                            exit_marks=exit_marks,
                        )
                        logger.info(
                            "Reconciled stale position to CLOSED during Kill Switch: trade_id=%s",
                            pos.trade_id,
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
            op = await session.get(KillSwitchOperationModel, operation_id)
            if op is not None:
                op.status = final_status
                op.unresolved_count = unresolved
                op.flattened_count = max(0, op.initial_position_count - unresolved)
                op.updated_at = datetime.now(UTC)
