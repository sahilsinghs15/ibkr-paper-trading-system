"""Generic multi-leg basket coordinator. Owns basket state, not per-leg OMS status."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from app.services.critical_recovery import CriticalRecoveryService

from app.db.models.signal import SignalModel
from app.db.repositories.basket_repository import BasketRepository
from app.db.repositories.event_repository import EventRepository
from app.db.repositories.execution_repository import ExecutionRepository
from app.db.repositories.order_repository import OrderRepository
from app.instruments.models import InstrumentResolutionError
from app.instruments.resolver import attach_resolved
from app.oms.basket import Basket, BasketExecutionResult, BasketState
from app.oms.models import OMSOrder, OMSOrderStatus
from app.oms.oms_service import OMSService
from app.oms.retry_policy import ExecutionRetryPolicy
from app.rms.engine import RMSEngine
from app.rms.models import (
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
    RMSContext,
    RMSOutcome,
    RMSResult,
)

logger = logging.getLogger(__name__)

_TERMINAL = (
    OMSOrderStatus.FILLED,
    OMSOrderStatus.CANCELLED,
    OMSOrderStatus.REJECTED,
    OMSOrderStatus.ERROR,
)
_FILL_EPS = 1e-8
SessionFactory = async_sessionmaker[AsyncSession]


class BasketCoordinator:
    """Submit N child orders, wait for terminal fills, compensate on basket failure.

    Child orders keep the OMS PENDING-until-broker-ack machine.
    """

    def __init__(
        self,
        oms: OMSService,
        *,
        session_factory: SessionFactory | None = None,
        fill_timeout: float = 10.0,
        cancel_timeout: float = 10.0,
        retry_policy: ExecutionRetryPolicy | None = None,
        rms_engine: RMSEngine | None = None,
        rms_context: RMSContext | None = None,
        paper_retries_allowed: bool = False,
    ) -> None:
        self._oms = oms
        self._session_factory = session_factory
        self._fill_timeout = fill_timeout
        self._cancel_timeout = cancel_timeout
        self._retry_policy = retry_policy
        self._rms_engine = rms_engine
        self._rms_context = rms_context
        self._paper_retries_allowed = paper_retries_allowed
        self._retry_ids: set[str] = set()
        self._critical: set[tuple[int, str]] = set()
        self._order_baskets: dict[str, Basket] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._recovery_service: CriticalRecoveryService | None = None
        adapter = getattr(oms, "_adapter", None)
        add_listener = getattr(adapter, "add_order_state_listener", None)
        if callable(add_listener):
            add_listener(self._on_broker_order_state)

    def is_open_blocked(self, account_id: int | None, strategy_id: str) -> bool:
        if account_id is None:
            return False
        return (account_id, strategy_id) in self._critical

    def mark_critical(self, account_id: int | None, strategy_id: str) -> None:
        if account_id is None:
            return
        self._critical.add((account_id, strategy_id))

    def set_recovery_service(self, service: CriticalRecoveryService) -> None:
        self._recovery_service = service

    async def clear_critical(
        self,
        *,
        account_id: int,
        strategy_id: str,
        trade_id: str,
        action: str,
        recovery_detail: str | None = None,
    ) -> bool:
        """Mark one basket RECOVERED and drop the OPEN latch when no CRITICAL rows remain."""
        if self._session_factory is None:
            return False
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            repo = BasketRepository(session)
            row = await repo.update_recovery(
                account_id=account_id,
                trade_id=trade_id,
                action=action,
                state=BasketState.RECOVERED.value,
                recovery_status="CLEARED",
                recovery_detail=recovery_detail or "Broker flat; OPEN latch cleared.",
                recovered_at=now,
            )
            if row is None:
                return False
            still_critical = await repo.has_critical(
                account_id=account_id, strategy_id=strategy_id
            )
        if not still_critical:
            self._critical.discard((account_id, strategy_id))
        basket = Basket(
            id=row.id,
            account_id=account_id,
            trade_id=trade_id,
            strategy_id=strategy_id,
            action=action,
            intended_leg_count=row.intended_leg_count,
            state=BasketState.RECOVERED,
            recovery_status="CLEARED",
            recovery_detail=recovery_detail,
            recovered_at=now,
        )
        await self._event(
            "BASKET_CRITICAL_CLEARED",
            {
                "account_id": account_id,
                "trade_id": trade_id,
                "strategy_id": strategy_id,
                "action": action,
            },
            basket=basket,
            idempotency_key=f"basket_critical_cleared:{account_id}:{trade_id}:{action}",
        )
        logger.info(
            "BASKET_CRITICAL_CLEARED: account_id=%s trade_id=%s strategy_id=%s still_latched=%s",
            account_id,
            trade_id,
            strategy_id,
            still_critical,
        )
        return True

    def apply_retry_policy(
        self,
        policy: ExecutionRetryPolicy,
        *,
        paper_retries_allowed: bool,
    ) -> None:
        policy.validate()
        self._retry_policy = policy
        self._fill_timeout = float(policy.square_off_after_sec)
        self._paper_retries_allowed = paper_retries_allowed
        logger.info(
            "Execution retry policy applied: enabled=%s timeout=%.1fs retries=%d "
            "interval=%.1fs window=%.1fs paper=%s",
            policy.enabled,
            policy.square_off_after_sec,
            policy.max_retries,
            policy.retry_interval_sec,
            policy.retry_window_sec,
            paper_retries_allowed,
        )

    async def hydrate_critical_from_db(self) -> None:
        if self._session_factory is None:
            return
        async with self._session_factory() as session:
            rows = await BasketRepository(session).list_critical()
            for row in rows:
                self._critical.add((row.account_id, row.strategy_id))
        if self._recovery_service is not None:
            await self._recovery_service.enqueue_all_critical()

    async def execute(
        self,
        intent: OrderIntent,
        rms_result: RMSResult,
        *,
        order_type: str,
        signal_pk: int | None = None,
    ) -> BasketExecutionResult:
        self._loop = asyncio.get_running_loop()
        try:
            intent = attach_resolved(intent)
        except InstrumentResolutionError as exc:
            raise ValueError(str(exc)) from exc
        trade_id = intent.signal_id
        action = intent.action.value if hasattr(intent.action, "value") else str(intent.action)
        basket = Basket(
            account_id=intent.account_id,
            trade_id=trade_id,
            strategy_id=intent.strategy_id,
            action=action,
            intended_leg_count=len(intent.legs),
            state=BasketState.EXECUTING,
            signal_pk=signal_pk,
        )
        await self._persist_basket(basket)
        await self._event(
            "BASKET_CREATED",
            {
                "account_id": intent.account_id,
                "trade_id": trade_id,
                "strategy_id": intent.strategy_id,
                "action": action,
                "legs": len(intent.legs),
            },
            signal_pk=signal_pk,
            basket=basket,
            idempotency_key=f"basket_created:{intent.account_id}:{trade_id}:{action}",
        )
        logger.info(
            "BASKET_CREATED: account_id=%s trade_id=%s strategy_id=%s action=%s legs=%d",
            intent.account_id,
            trade_id,
            intent.strategy_id,
            action,
            len(intent.legs),
        )
        await self._event(
            "BASKET_EXECUTING",
            {
                "account_id": intent.account_id,
                "trade_id": trade_id,
                "strategy_id": intent.strategy_id,
                "legs": len(intent.legs),
            },
            signal_pk=signal_pk,
            basket=basket,
            idempotency_key=f"basket_executing:{intent.account_id}:{trade_id}:{action}",
        )
        logger.info(
            "BASKET_EXECUTING: account_id=%s trade_id=%s legs=%d",
            intent.account_id,
            trade_id,
            len(intent.legs),
        )

        submitted: list[OMSOrder] = []
        abort_remaining = False
        received_at = datetime.now(UTC)
        for index, _leg in enumerate(intent.legs):
            if abort_remaining:
                break
            order = await self._oms.submit_one_leg(
                intent,
                rms_result,
                index,
                oms_received_at=received_at,
                order_type=order_type,
            )
            submitted.append(order)
            self._order_baskets[order.internal_order_id] = basket
            await self._persist_child(order, intent, signal_pk=signal_pk, basket=basket)
            close = action == "CLOSE"
            created_kind = "CLOSE_ORDER_CREATED" if close else "ORDER_CREATED"
            submit_kind = "CLOSE_ORDER_SUBMITTED" if close else "ORDER_SUBMITTED"
            await self._event(
                created_kind,
                {
                    "account_id": intent.account_id,
                    "trade_id": trade_id,
                    "internal_order_id": order.internal_order_id,
                    "symbol": order.symbol,
                    "quantity": order.quantity,
                    "side": order.side.value if hasattr(order.side, "value") else str(order.side),
                },
                signal_pk=signal_pk,
                basket=basket,
                order=order,
                idempotency_key=f"order_created:{order.internal_order_id}",
            )
            logger.info(
                "%s: internal_order_id=%s symbol=%s side=%s qty=%s status=%s",
                created_kind,
                order.internal_order_id,
                order.symbol,
                order.side.value if hasattr(order.side, "value") else order.side,
                order.quantity,
                order.status.value,
            )
            if order.ibkr_order_id is not None:
                await self._event(
                    submit_kind,
                    {
                        "account_id": intent.account_id,
                        "trade_id": trade_id,
                        "internal_order_id": order.internal_order_id,
                        "broker_order_id": str(order.ibkr_order_id),
                    },
                    signal_pk=signal_pk,
                    basket=basket,
                    order=order,
                    idempotency_key=f"order_submitted:{order.internal_order_id}",
                )
                logger.info(
                    "%s: internal_order_id=%s broker_order_id=%s symbol=%s",
                    submit_kind,
                    order.internal_order_id,
                    order.ibkr_order_id,
                    order.symbol,
                )
            if order.status in (OMSOrderStatus.REJECTED, OMSOrderStatus.ERROR):
                abort_remaining = True

        await self._wait_terminals(submitted, timeout=self._fill_timeout)
        for order in submitted:
            await self._persist_child(order, intent, signal_pk=signal_pk, basket=basket)

        completeness = [
            {
                "leg_index": o.leg_index,
                "internal_order_id": o.internal_order_id,
                "symbol": o.symbol,
                "status": o.status.value,
                "intended_qty": float(intent.legs[o.leg_index].quantity)
                if o.leg_index is not None and o.leg_index < len(intent.legs)
                else None,
                "filled_qty": o.filled_quantity,
            }
            for o in submitted
        ]
        logger.info(
            "Basket fill wait complete: trade_id=%s complete=%s legs=%s",
            trade_id,
            self._basket_complete(intent, submitted),
            completeness,
        )

        if not self._basket_complete(intent, submitted):
            retry_orders = await self._retry_incomplete(
                intent,
                submitted,
                order_type=order_type,
                signal_pk=signal_pk,
                basket=basket,
            )
            if retry_orders:
                submitted.extend(retry_orders)
                for order in retry_orders:
                    await self._persist_child(order, intent, signal_pk=signal_pk, basket=basket)

        if self._basket_complete(intent, submitted):
            basket.orders = submitted
            if intent.action == OrderAction.CLOSE:
                basket.state = BasketState.CLOSED
                event_kind = "BASKET_CLOSED"
            else:
                basket.state = BasketState.OPEN
                event_kind = "BASKET_OPEN"
            await self._persist_basket(basket)
            for order in submitted:
                await self._persist_child(order, intent, signal_pk=signal_pk, basket=basket)
            await self._event(
                event_kind,
                {"account_id": intent.account_id, "trade_id": trade_id},
                signal_pk=signal_pk,
                basket=basket,
                idempotency_key=f"{event_kind.lower()}:{intent.account_id}:{trade_id}",
            )
            logger.info(
                "%s: account_id=%s trade_id=%s orders=%d",
                event_kind,
                intent.account_id,
                trade_id,
                len(submitted),
            )
            return BasketExecutionResult(basket=basket, intent=intent, orders=submitted)

        basket.state = BasketState.UNWINDING
        await self._persist_basket(basket)
        await self._event(
            "BASKET_UNWINDING",
            {"account_id": intent.account_id, "trade_id": trade_id},
            signal_pk=signal_pk,
            basket=basket,
        )
        logger.warning(
            "BASKET_UNWINDING: account_id=%s trade_id=%s incomplete legs=%s",
            intent.account_id,
            trade_id,
            completeness,
        )

        working = [o for o in submitted if o.status not in _TERMINAL]
        for order in working:
            try:
                await self._oms.cancel_order(order.internal_order_id)
            except Exception as exc:
                logger.warning("Cancel failed for %s: %s", order.internal_order_id, exc)
                basket.state = BasketState.CRITICAL
                await self._fail_critical(basket, intent, submitted, [], signal_pk)
                return BasketExecutionResult(
                    basket=basket, intent=intent, orders=submitted
                )
        await self._wait_terminals(working, timeout=self._cancel_timeout)
        if any(o.status not in _TERMINAL for o in working):
            basket.state = BasketState.CRITICAL
            await self._fail_critical(basket, intent, submitted, [], signal_pk)
            return BasketExecutionResult(basket=basket, intent=intent, orders=submitted)

        for order in submitted:
            await self._persist_child(order, intent, signal_pk=signal_pk, basket=basket)

        compensation: list[OMSOrder] = []
        try:
            compensation = await self._compensate_filled(
                intent, submitted, order_type=order_type, signal_pk=signal_pk, basket=basket
            )
        except Exception:
            logger.exception("Compensation failed for trade_id=%s", trade_id)
            basket.state = BasketState.CRITICAL
            await self._fail_critical(basket, intent, submitted, compensation, signal_pk)
            return BasketExecutionResult(
                basket=basket,
                intent=intent,
                orders=submitted,
                compensation_orders=compensation,
            )

        await self._wait_terminals(compensation, timeout=self._fill_timeout)
        if not self._compensation_complete(compensation):
            basket.state = BasketState.CRITICAL
            await self._fail_critical(basket, intent, submitted, compensation, signal_pk)
            return BasketExecutionResult(
                basket=basket,
                intent=intent,
                orders=submitted,
                compensation_orders=compensation,
            )

        basket.orders = submitted
        basket.compensation_orders = compensation
        basket.state = BasketState.COMPENSATED
        await self._persist_basket(basket)
        await self._event(
            "BASKET_COMPENSATED",
            {"account_id": intent.account_id, "trade_id": trade_id},
            signal_pk=signal_pk,
            basket=basket,
            idempotency_key=f"basket_compensated:{intent.account_id}:{trade_id}",
        )
        logger.info(
            "BASKET_COMPENSATED: account_id=%s trade_id=%s compensation_orders=%d",
            intent.account_id,
            trade_id,
            len(compensation),
        )
        return BasketExecutionResult(
            basket=basket,
            intent=intent,
            orders=submitted,
            compensation_orders=compensation,
        )

    async def recover_incomplete_baskets(self) -> None:
        """Restart: incomplete baskets become CRITICAL unless broker state is known."""
        if self._session_factory is None:
            return
        adapter = self._oms._adapter
        async with self._session_factory() as session:
            rows = await BasketRepository(session).list_incomplete()
        if not rows:
            return
        snapshot_ok = adapter.fetch_broker_order_snapshot()
        for row in rows:
            if not snapshot_ok:
                basket = Basket(
                    id=row.id,
                    account_id=row.account_id,
                    trade_id=row.trade_id,
                    strategy_id=row.strategy_id,
                    action=row.action,
                    intended_leg_count=row.intended_leg_count,
                    state=BasketState.CRITICAL,
                )
                await self._persist_basket(basket)
                self.mark_critical(row.account_id, row.strategy_id)
                await self._event(
                    "BASKET_RECOVER_CRITICAL",
                    {
                        "account_id": row.account_id,
                        "trade_id": row.trade_id,
                        "reason": "broker_snapshot_unavailable",
                    },
                )
                logger.warning(
                    "BASKET_RECOVER_CRITICAL: account_id=%s trade_id=%s reason=broker_snapshot_unavailable",
                    row.account_id,
                    row.trade_id,
                )
                continue
            # Broker snapshot requested; without deterministic fill application
            # still escalate incomplete rows to CRITICAL.
            basket = Basket(
                id=row.id,
                account_id=row.account_id,
                trade_id=row.trade_id,
                strategy_id=row.strategy_id,
                action=row.action,
                intended_leg_count=row.intended_leg_count,
                state=BasketState.CRITICAL,
            )
            await self._persist_basket(basket)
            self.mark_critical(row.account_id, row.strategy_id)

    def _filled_qty_for_leg(self, index: int, orders: list[OMSOrder]) -> float:
        return sum(
            float(o.filled_quantity)
            for o in orders
            if o.leg_index == index and not o.is_compensation
        )

    def _basket_complete(self, intent: OrderIntent, orders: list[OMSOrder]) -> bool:
        for index, leg in enumerate(intent.legs):
            filled = self._filled_qty_for_leg(index, orders)
            if filled + _FILL_EPS < float(leg.quantity):
                return False
        return True

    def _compensation_complete(self, orders: list[OMSOrder]) -> bool:
        if not orders:
            return True
        return all(
            o.status == OMSOrderStatus.FILLED
            and o.filled_quantity + _FILL_EPS >= o.quantity
            for o in orders
        )

    async def _wait_terminals(self, orders: list[OMSOrder], *, timeout: float) -> None:
        adapter = self._oms._adapter

        async def _wait_one(order: OMSOrder) -> None:
            if order.status in _TERMINAL:
                return
            try:
                await adapter.wait_for_terminal_or_fill(
                    order.internal_order_id, timeout=timeout
                )
            except TimeoutError:
                logger.warning(
                    "Timed out waiting for terminal state: %s status=%s filled=%s",
                    order.internal_order_id,
                    order.status.value,
                    order.filled_quantity,
                )
            except ValueError:
                logger.warning(
                    "Order %s not tracked by adapter while waiting",
                    order.internal_order_id,
                )

        await asyncio.gather(*[_wait_one(order) for order in orders])

    def _retries_enabled(self) -> bool:
        policy = self._retry_policy
        if policy is None or not policy.enabled or policy.max_retries <= 0:
            return False
        if not self._paper_retries_allowed:
            logger.info(
                "AUTO-SQUARE-OFF retries skipped: paper ports only (IBKR live port or flag off)"
            )
            return False
        if self._rms_engine is None or self._rms_context is None:
            logger.warning("AUTO-SQUARE-OFF retries skipped: RMS engine not wired")
            return False
        return True

    async def _cancel_working(
        self,
        submitted: list[OMSOrder],
        *,
        intent: OrderIntent,
        signal_pk: int | None,
        basket: Basket,
    ) -> bool:
        working = [o for o in submitted if o.status not in _TERMINAL]
        for order in working:
            try:
                await self._oms.cancel_order(order.internal_order_id)
            except Exception as exc:
                logger.warning("Cancel failed for %s: %s", order.internal_order_id, exc)
                return False
        await self._wait_terminals(working, timeout=self._cancel_timeout)
        if any(o.status not in _TERMINAL for o in working):
            return False
        for order in submitted:
            await self._persist_child(order, intent, signal_pk=signal_pk, basket=basket)
        return True

    async def _retry_incomplete(
        self,
        intent: OrderIntent,
        submitted: list[OMSOrder],
        *,
        order_type: str,
        signal_pk: int | None,
        basket: Basket,
    ) -> list[OMSOrder]:
        if not self._retries_enabled():
            return []
        policy = self._retry_policy
        assert policy is not None
        cancelled = await self._cancel_working(
            submitted, intent=intent, signal_pk=signal_pk, basket=basket
        )
        if not cancelled:
            logger.warning(
                "AUTO-SQUARE-OFF retry aborted: could not cancel working orders trade_id=%s",
                intent.signal_id,
            )
            return []

        created: list[OMSOrder] = []
        rms_blocked: set[int] = set()
        started = time.monotonic()
        attempt = 0
        while attempt < policy.max_retries:
            elapsed = time.monotonic() - started
            if elapsed >= policy.retry_window_sec:
                logger.info(
                    "AUTO-SQUARE-OFF retry window expired: trade_id=%s elapsed=%.1fs window=%.1fs",
                    intent.signal_id,
                    elapsed,
                    policy.retry_window_sec,
                )
                break
            pool = submitted + created
            if self._basket_complete(intent, pool):
                break
            if attempt > 0:
                cancelled = await self._cancel_working(
                    submitted + created, intent=intent, signal_pk=signal_pk, basket=basket
                )
                if not cancelled:
                    logger.warning(
                        "AUTO-SQUARE-OFF retry step aborted: could not cancel working orders trade_id=%s attempt=%d",
                        intent.signal_id,
                        attempt,
                    )
                    break
                await asyncio.sleep(policy.retry_interval_sec)
                elapsed = time.monotonic() - started
                if elapsed >= policy.retry_window_sec:
                    break
            attempt += 1
            round_orders: list[OMSOrder] = []
            for index, orig_leg in enumerate(intent.legs):
                if index in rms_blocked:
                    continue
                filled = self._filled_qty_for_leg(index, submitted + created)
                remaining = float(orig_leg.quantity) - filled
                if remaining <= _FILL_EPS:
                    continue
                retry_key = f"{intent.account_id}:{intent.signal_id}:{index}:{attempt}"
                if retry_key in self._retry_ids:
                    logger.info(
                        "AUTO-SQUARE-OFF skip duplicate retry key=%s",
                        retry_key,
                    )
                    continue
                self._retry_ids.add(retry_key)
                retry_intent = self._retry_intent(intent, orig_leg, remaining, index, attempt)
                rms_result = self._rms_engine.evaluate(retry_intent, self._rms_context)
                rate_limited = False
                if rms_result.outcome != RMSOutcome.PASS:
                    rms_blocked.add(index)
                    logger.warning(
                        "AUTO-SQUARE-OFF retry blocked: trade_id=%s basket_id=%s account_id=%s "
                        "ibkr_account=%s symbol=%s instrument_type=%s original_qty=%s "
                        "filled_qty=%s remaining_qty=%s retry=%d/%d retry_window=%.1fs "
                        "elapsed=%.1fs reason=incomplete_leg rms=REJECT reason=%s "
                        "rate_limit=N/A action=SKIP",
                        intent.signal_id,
                        basket.id,
                        intent.account_id,
                        intent.ibkr_account,
                        orig_leg.symbol,
                        orig_leg.instrument_type,
                        orig_leg.quantity,
                        filled,
                        remaining,
                        attempt,
                        policy.max_retries,
                        policy.retry_window_sec,
                        time.monotonic() - started,
                        rms_result.reason,
                    )
                    await self._event(
                        "AUTO_SQUARE_OFF_RETRY_BLOCKED",
                        {
                            "trade_id": intent.signal_id,
                            "symbol": orig_leg.symbol,
                            "remaining_qty": remaining,
                            "retry": attempt,
                            "reason": rms_result.reason,
                        },
                        signal_pk=signal_pk,
                        basket=basket,
                        idempotency_key=f"retry_blocked:{retry_key}",
                    )
                    continue
                try:
                    order = await self._oms.submit_one_leg(
                        retry_intent,
                        rms_result,
                        0,
                        order_type=order_type,
                    )
                except Exception:
                    logger.exception(
                        "AUTO-SQUARE-OFF retry submit failed: trade_id=%s symbol=%s",
                        intent.signal_id,
                        orig_leg.symbol,
                    )
                    continue
                order.leg_index = index
                order.basket_id = basket.id
                self._order_baskets[order.internal_order_id] = basket
                rate_limited = bool(getattr(order, "pacer_delayed", False))
                logger.info(
                    "AUTO-SQUARE-OFF retry: trade_id=%s basket_id=%s account_id=%s "
                    "ibkr_account=%s symbol=%s instrument_type=%s original_qty=%s "
                    "filled_qty=%s remaining_qty=%s retry=%d/%d retry_window=%.1fs "
                    "elapsed=%.1fs reason=incomplete_leg rms=PASS rate_limit=%s "
                    "action=SUBMIT broker_order_id=%s",
                    intent.signal_id,
                    basket.id,
                    intent.account_id,
                    intent.ibkr_account,
                    orig_leg.symbol,
                    orig_leg.instrument_type,
                    orig_leg.quantity,
                    filled,
                    remaining,
                    attempt,
                    policy.max_retries,
                    policy.retry_window_sec,
                    time.monotonic() - started,
                    "DELAYED" if rate_limited else "PASS",
                    order.ibkr_order_id,
                )
                await self._event(
                    "AUTO_SQUARE_OFF_RETRY",
                    {
                        "trade_id": intent.signal_id,
                        "symbol": orig_leg.symbol,
                        "remaining_qty": remaining,
                        "retry": attempt,
                        "broker_order_id": str(order.ibkr_order_id)
                        if order.ibkr_order_id
                        else None,
                    },
                    signal_pk=signal_pk,
                    basket=basket,
                    order=order,
                    idempotency_key=f"retry_submit:{retry_key}",
                )
                created.append(order)
                round_orders.append(order)
            if round_orders:
                remaining_window = policy.retry_window_sec - (time.monotonic() - started)
                retry_timeout = min(self._fill_timeout, max(0.05, remaining_window))
                await self._wait_terminals(
                    round_orders, timeout=retry_timeout
                )
        return created

    def _retry_intent(
        self,
        original: OrderIntent,
        orig_leg: OrderLeg,
        remaining: float,
        index: int,
        attempt: int,
    ) -> OrderIntent:
        qty = Decimal(str(remaining))
        px = orig_leg.price
        retry_leg = replace(
            orig_leg,
            quantity=qty,
            notional=qty * px,
            leg_index=index,
        )
        from app.rms.models import ExecutionIntentMode
        mode = (
            ExecutionIntentMode.EMERGENCY_FLATTEN
            if getattr(original, "intent_mode", None) == ExecutionIntentMode.EMERGENCY_FLATTEN or original.action == OrderAction.CLOSE
            else getattr(original, "intent_mode", ExecutionIntentMode.OPEN)
        )
        return replace(
            original,
            signal_id=f"{original.signal_id}:RETRY:L{index}:{attempt}",
            legs=[retry_leg],
            timestamp=datetime.now(UTC),
            intent_mode=mode,
        )

    async def _compensate_filled(
        self,
        original: OrderIntent,
        submitted: list[OMSOrder],
        *,
        order_type: str,
        signal_pk: int | None,
        basket: Basket,
    ) -> list[OMSOrder]:
        created: list[OMSOrder] = []
        now = datetime.now(UTC)
        for orig_index, orig_leg in enumerate(original.legs):
            cum_filled = self._filled_qty_for_leg(orig_index, submitted)
            if cum_filled <= _FILL_EPS:
                continue
            reverse = OrderSide.SELL if orig_leg.side == OrderSide.BUY else OrderSide.BUY
            price = orig_leg.price or Decimal(0)
            comp_intent = OrderIntent(
                signal_id=f"{original.signal_id}:UNWIND:L{orig_index}",
                strategy_id=original.strategy_id,
                action=OrderAction.CLOSE,
                account_id=original.account_id,
                ibkr_account=original.ibkr_account,
                market=original.market,
                legs=[
                    OrderLeg(
                        symbol=orig_leg.symbol,
                        side=reverse,
                        quantity=Decimal(str(cum_filled)),
                        price=price if isinstance(price, Decimal) else Decimal(str(price)),
                        contract_month=orig_leg.contract_month,
                        instrument_type=orig_leg.instrument_type,
                        con_id=orig_leg.con_id,
                        exchange=orig_leg.exchange,
                        currency=orig_leg.currency,
                        resolved=orig_leg.resolved,
                        leg_index=orig_index,
                    )
                ],
                timestamp=now,
            )
            pass_rms = RMSResult(
                outcome=RMSOutcome.PASS,
                intent=comp_intent,
                original_intent=comp_intent,
                timestamp=now,
            )
            exec_res = await self._oms.submit_intent(
                comp_intent, pass_rms, order_type=order_type
            )
            matching_orders = [o for o in submitted if o.leg_index == orig_index and not o.is_compensation]
            comp_of = matching_orders[0].internal_order_id if matching_orders else f"cumulative:L{orig_index}"
            for child in exec_res.orders:
                child.leg_index = orig_index
                child.is_compensation = True
                child.compensation_of_internal_order_id = comp_of
                child.basket_id = basket.id
                created.append(child)
                self._order_baskets[child.internal_order_id] = basket
                await self._persist_child(
                    child, original, signal_pk=signal_pk, basket=basket, compensation=True
                )
                await self._event(
                    "COMPENSATION",
                    {
                        "account_id": original.account_id,
                        "trade_id": original.signal_id,
                        "internal_order_id": child.internal_order_id,
                        "of": comp_of,
                        "quantity": child.quantity,
                        "side": child.side.value if hasattr(child.side, "value") else str(child.side),
                        "symbol": child.symbol,
                    },
                    signal_pk=signal_pk,
                    basket=basket,
                    order=child,
                    idempotency_key=f"compensation:{child.internal_order_id}",
                )
                logger.warning(
                    "COMPENSATION: internal_order_id=%s symbol=%s qty=%s side=%s of=%s",
                    child.internal_order_id,
                    child.symbol,
                    child.quantity,
                    child.side.value if hasattr(child.side, "value") else child.side,
                    comp_of,
                )
                if child.status in (OMSOrderStatus.REJECTED, OMSOrderStatus.ERROR):
                    raise RuntimeError(
                        f"Compensation submit failed for cumulative:L{orig_index}: {child.error_message}"
                    )
        return created

    async def _fail_critical(
        self,
        basket: Basket,
        intent: OrderIntent,
        submitted: list[OMSOrder],
        compensation: list[OMSOrder],
        signal_pk: int | None,
    ) -> None:
        basket.state = BasketState.CRITICAL
        basket.recovery_status = "RECOVERING"
        self.mark_critical(intent.account_id, intent.strategy_id)
        await self._persist_basket(basket)
        for order in submitted + compensation:
            await self._persist_child(order, intent, signal_pk=signal_pk, basket=basket)
        await self._event(
            "BASKET_CRITICAL",
            {
                "account_id": intent.account_id,
                "trade_id": intent.signal_id,
                "strategy_id": intent.strategy_id,
            },
            signal_pk=signal_pk,
            basket=basket,
            idempotency_key=f"basket_critical:{intent.account_id}:{intent.signal_id}",
        )
        logger.error(
            "BASKET_CRITICAL: account_id=%s trade_id=%s strategy_id=%s — new OPENs blocked",
            intent.account_id,
            intent.signal_id,
            intent.strategy_id,
        )
        if self._recovery_service is not None and intent.account_id is not None:
            self._recovery_service.schedule_recovery(
                account_id=intent.account_id,
                trade_id=intent.signal_id,
                action=basket.action,
                strategy_id=intent.strategy_id,
            )

    async def _persist_basket(self, basket: Basket) -> None:
        if self._session_factory is None or basket.account_id is None:
            return
        async with self._session_factory() as session, session.begin():
            row = await BasketRepository(session).upsert(
                account_id=basket.account_id,
                trade_id=basket.trade_id,
                strategy_id=basket.strategy_id,
                action=basket.action,
                state=basket.state.value,
                intended_leg_count=basket.intended_leg_count,
                recovery_status=basket.recovery_status,
                recovery_detail=basket.recovery_detail,
                recovered_at=basket.recovered_at,
            )
            basket.id = row.id

    async def _persist_child(
        self,
        order: OMSOrder,
        intent: OrderIntent,
        *,
        signal_pk: int | None,
        basket: Basket,
        compensation: bool = False,
    ) -> None:
        if compensation:
            order.is_compensation = True
        order.basket_id = basket.id
        if self._session_factory is None or intent.account_id is None:
            return
        pk = signal_pk
        async with self._session_factory() as session, session.begin():
            if pk is None:
                pk = await self._ensure_signal_pk(session, intent)
            await OrderRepository(session).record_oms_order(
                order,
                signal_pk=pk,
                account_id=intent.account_id,
                trade_id=intent.signal_id.split(":UNWIND:")[0],
                strategy_id=intent.strategy_id,
                leg_label=f"L{order.leg_index if order.leg_index is not None else 0}",
            )
            order_row = await OrderRepository(session).get_by_internal_id(order.internal_order_id)
            exec_repo = ExecutionRepository(session)
            for execution in order.executions.values():
                await exec_repo.upsert(
                    execution,
                    order_id=order_row.id if order_row is not None else None,
                    account_id=intent.account_id,
                )

    async def _ensure_signal_pk(self, session: AsyncSession, intent: OrderIntent) -> int:
        from sqlalchemy import select
        from sqlalchemy.dialects.postgresql import insert

        trade_id = intent.signal_id.split(":UNWIND:")[0].split(":CLOSE")[0]
        persist_id = intent.signal_id.split(":UNWIND:")[0]
        legs = list(intent.legs or [])
        pair = ":".join(leg.symbol for leg in legs) if legs else ""
        if legs:
            side = (
                legs[0].side.value if hasattr(legs[0].side, "value") else str(legs[0].side)
            )
            ref_a = legs[0].price
            ref_b = legs[1].price if len(legs) > 1 else None
        else:
            side = "N/A"
            ref_a = Decimal(0)
            ref_b = None
        existing = (
            await session.execute(
                select(SignalModel).where(
                    SignalModel.strategy_id == intent.strategy_id,
                    SignalModel.signal_id == persist_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing.id
        logger.warning(
            "Inserting signal FK row without webhook capture: strategy_id=%s "
            "signal_id=%s pair=%s (original TradingView JSON was not supplied on this path)",
            intent.strategy_id,
            persist_id,
            pair,
        )
        stmt = (
            insert(SignalModel)
            .values(
                strategy_id=intent.strategy_id,
                signal_id=persist_id,
                trade_id=trade_id,
                action=intent.action.value
                if hasattr(intent.action, "value")
                else str(intent.action),
                pair=pair,
                side=side,
                ref_price_a=ref_a,
                ref_price_b=ref_b,
                raw_payload={
                    "source": "oms_signal_fk",
                    "trade_id": trade_id,
                    "strategy_id": intent.strategy_id,
                    "legs": [
                        {
                            "symbol": leg.symbol,
                            "side": leg.side.value
                            if hasattr(leg.side, "value")
                            else str(leg.side),
                            "quantity": str(leg.quantity),
                            "price": str(leg.price),
                            "instrument_type": getattr(leg, "instrument_type", None),
                        }
                        for leg in legs
                    ],
                },
                status="NEW",
            )
            .on_conflict_do_nothing(constraint="uq_signals_strategy_signal")
        )
        await session.execute(stmt)
        await session.flush()
        existing = (
            await session.execute(
                select(SignalModel).where(
                    SignalModel.strategy_id == intent.strategy_id,
                    SignalModel.signal_id == persist_id,
                )
            )
        ).scalar_one()
        return existing.id

    def _on_broker_order_state(self, order: OMSOrder, kind: str) -> None:
        if kind not in (
            "BROKER_ACK",
            "PARTIAL_FILL",
            "FILL",
            "COMMISSION",
            "REJECTED",
            "CANCELLED",
            "ERROR",
        ):
            return
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._persist_broker_snapshot(order, kind), loop
            )
        except Exception:
            logger.exception("Failed to schedule broker snapshot persist")

    def _event_idempotency_key(self, kind: str, order: OMSOrder | None, detail: dict) -> str | None:
        if kind == "BROKER_ACK" and order is not None:
            return f"broker_ack:{order.internal_order_id}"
        if kind == "FILL" and order is not None:
            return f"fill:{order.internal_order_id}"
        if kind == "PARTIAL_FILL" and order is not None:
            exec_id = order.last_exec_id
            if exec_id:
                return f"partial:{exec_id}"
            return f"partial:{order.internal_order_id}:{order.filled_quantity}"
        if kind == "COMMISSION" and order is not None and order.last_exec_id:
            return f"commission:{order.last_exec_id}"
        return None

    async def _persist_broker_snapshot(self, order: OMSOrder, kind: str) -> None:
        basket = self._order_baskets.get(order.internal_order_id)
        if basket is None:
            basket = Basket(
                account_id=order.intent.account_id,
                trade_id=order.intent.signal_id.split(":UNWIND:")[0],
                strategy_id=order.intent.strategy_id,
                action=order.intent.action.value
                if hasattr(order.intent.action, "value")
                else str(order.intent.action),
                intended_leg_count=len(order.intent.legs),
                id=order.basket_id,
            )
        signal_pk = basket.signal_pk
        await self._persist_child(order, order.intent, signal_pk=signal_pk, basket=basket)
        if kind == "COMMISSION":
            return
        await self._event(
            kind,
            {
                "account_id": order.intent.account_id,
                "trade_id": order.intent.signal_id.split(":UNWIND:")[0],
                "internal_order_id": order.internal_order_id,
                "broker_order_id": str(order.ibkr_order_id) if order.ibkr_order_id else None,
                "status": order.status.value,
                "filled_quantity": order.filled_quantity,
                "average_fill_price": str(order.average_fill_price)
                if order.average_fill_price is not None
                else None,
                "exec_id": order.last_exec_id,
            },
            signal_pk=signal_pk,
            basket=basket,
            order=order,
            idempotency_key=self._event_idempotency_key(kind, order, {}),
        )

    async def _event(
        self,
        kind: str,
        detail: dict,
        *,
        signal_pk: int | None = None,
        basket: Basket | None = None,
        order: OMSOrder | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        if self._session_factory is None:
            return
        try:
            async with self._session_factory() as session, session.begin():
                order_pk = None
                if order is not None:
                    row = await OrderRepository(session).get_by_internal_id(
                        order.internal_order_id
                    )
                    order_pk = row.id if row is not None else None
                await EventRepository(session).append(
                    process="basket",
                    kind=kind,
                    detail=detail,
                    signal_id=signal_pk,
                    order_id=order_pk,
                    basket_id=basket.id if basket is not None else None,
                    idempotency_key=idempotency_key,
                )
        except Exception:
            logger.exception("Failed to write event_log kind=%s", kind)
