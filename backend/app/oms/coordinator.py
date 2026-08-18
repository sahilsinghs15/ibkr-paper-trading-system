"""Generic multi-leg basket coordinator. Owns basket state, not per-leg OMS status."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.signal import SignalModel
from app.db.repositories.basket_repository import BasketRepository
from app.db.repositories.event_repository import EventRepository
from app.db.repositories.execution_repository import ExecutionRepository
from app.db.repositories.order_repository import OrderRepository
from app.instruments.resolver import attach_resolved
from app.instruments.models import InstrumentResolutionError
from app.oms.basket import Basket, BasketExecutionResult, BasketState
from app.oms.models import OMSOrder, OMSOrderStatus
from app.oms.oms_service import OMSService
from app.rms.models import (
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
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
    ) -> None:
        self._oms = oms
        self._session_factory = session_factory
        self._fill_timeout = fill_timeout
        self._cancel_timeout = cancel_timeout
        self._critical: set[tuple[int, str]] = set()
        self._order_baskets: dict[str, Basket] = {}
        self._active_basket: Basket | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
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

    async def hydrate_critical_from_db(self) -> None:
        if self._session_factory is None:
            return
        async with self._session_factory() as session:
            rows = await BasketRepository(session).list_critical()
            for row in rows:
                self._critical.add((row.account_id, row.strategy_id))

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
        self._active_basket = basket
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
            if order.status in (OMSOrderStatus.REJECTED, OMSOrderStatus.ERROR):
                abort_remaining = True

        await self._wait_terminals(submitted, timeout=self._fill_timeout)
        for order in submitted:
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
            return BasketExecutionResult(basket=basket, intent=intent, orders=submitted)

        basket.state = BasketState.UNWINDING
        await self._persist_basket(basket)
        await self._event(
            "BASKET_UNWINDING",
            {"account_id": intent.account_id, "trade_id": trade_id},
            signal_pk=signal_pk,
            basket=basket,
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

    def _basket_complete(self, intent: OrderIntent, orders: list[OMSOrder]) -> bool:
        if len(orders) != len(intent.legs):
            return False
        by_index = {o.leg_index: o for o in orders}
        for index, leg in enumerate(intent.legs):
            order = by_index.get(index)
            if order is None or order.status != OMSOrderStatus.FILLED:
                return False
            if order.filled_quantity + _FILL_EPS < float(leg.quantity):
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
        for order in submitted:
            filled = float(order.filled_quantity)
            if filled <= _FILL_EPS:
                continue
            reverse = OrderSide.SELL if order.side == OrderSide.BUY else OrderSide.BUY
            price = order.average_fill_price or order.last_fill_price or order.limit_price or Decimal("0")
            orig_index = order.leg_index if order.leg_index is not None else 0
            orig_leg = original.legs[orig_index]
            comp_intent = OrderIntent(
                signal_id=f"{original.signal_id}:UNWIND:L{orig_index}",
                strategy_id=original.strategy_id,
                action=OrderAction.CLOSE,
                account_id=original.account_id,
                ibkr_account=original.ibkr_account,
                market=original.market,
                legs=[
                    OrderLeg(
                        symbol=order.symbol,
                        side=reverse,
                        quantity=filled,
                        price=price if isinstance(price, Decimal) else Decimal(str(price)),
                        contract_month=orig_leg.contract_month,
                        instrument_type=orig_leg.instrument_type,
                        con_id=orig_leg.con_id,
                        exchange=orig_leg.exchange,
                        currency=orig_leg.currency,
                        resolved=orig_leg.resolved,
                        leg_index=0,
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
            for child in exec_res.orders:
                child.is_compensation = True
                child.compensation_of_internal_order_id = order.internal_order_id
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
                        "of": order.internal_order_id,
                        "quantity": child.quantity,
                        "symbol": child.symbol,
                    },
                )
                if child.status in (OMSOrderStatus.REJECTED, OMSOrderStatus.ERROR):
                    raise RuntimeError(
                        f"Compensation submit failed for {order.internal_order_id}: {child.error_message}"
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
        basket = self._order_baskets.get(order.internal_order_id) or self._active_basket
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
