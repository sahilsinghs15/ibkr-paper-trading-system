"""Order Management System (OMS) service orchestrating order lifecycle and broker execution."""

import logging
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from app.instruments.models import InstrumentResolutionError
from app.instruments.resolver import resolve_leg
from app.oms.ibkr_adapter import IBKRExecutionAdapter
from app.oms.models import ExecutionResult, OMSOrder, OMSOrderStatus
from app.rms.models import OrderIntent, OrderLeg, RMSOutcome, RMSResult

logger = logging.getLogger(__name__)


class OMSService:
    """Minimal Order Management System service.

    Responsible for:
      1. Validating RMS PASS status on incoming OrderIntents.
      2. Constructing internal OMSOrder representations for every intent leg.
      3. Submitting each leg through an injected IBKRExecutionAdapter.
      4. Managing order lifecycle state and timestamp instrumentation.
    """

    def __init__(self, adapter: IBKRExecutionAdapter) -> None:
        """Initialize OMSService with an IBKR execution adapter."""
        self._adapter = adapter
        self._orders: dict[str, OMSOrder] = {}
        self._submitted_signals: set[str] = set()

    def get_order(self, internal_order_id: str) -> OMSOrder | None:
        """Retrieve tracked order by internal order ID."""
        return self._orders.get(internal_order_id)

    def get_all_orders(self) -> list[OMSOrder]:
        """Return list of all tracked internal orders."""
        return list(self._orders.values())

    async def submit_intent(
        self,
        intent: OrderIntent,
        rms_result: RMSResult,
        override_internal_id: str | None = None,
        limit_price: Decimal | None = None,
        order_type: str = "LIMIT",
    ) -> ExecutionResult:
        """Process an OrderIntent that has undergone RMS evaluation.

        Submits every intent leg as an independent broker order sharing
        the same parent signal_id / trade identity.
        """
        oms_received_at = datetime.now(UTC)

        if rms_result.outcome != RMSOutcome.PASS:
            logger.warning(
                "OMS rejected intent %s: RMS outcome is %s (reason: %s)",
                intent.signal_id,
                rms_result.outcome.value,
                rms_result.reason,
            )
            rejected_order = self._create_rejected_order(
                intent=intent,
                reason=f"RMS check failed with outcome {rms_result.outcome.value}: {rms_result.reason}",
                oms_received_at=oms_received_at,
                override_internal_id=override_internal_id,
            )
            return ExecutionResult(
                order=rejected_order,
                rms_result=rms_result,
                success=False,
                error_message=rejected_order.error_message,
                orders=[rejected_order],
            )

        duplicate_key = f"{intent.account_id}:{intent.signal_id}"
        if duplicate_key in self._submitted_signals:
            msg = f"Duplicate intent submission attempt for signal_id: {intent.signal_id}"
            logger.error(msg)
            rejected_order = self._create_rejected_order(
                intent=intent,
                reason=msg,
                oms_received_at=oms_received_at,
                override_internal_id=override_internal_id,
            )
            return ExecutionResult(
                order=rejected_order,
                rms_result=rms_result,
                success=False,
                error_message=msg,
                orders=[rejected_order],
            )

        if not intent.legs:
            msg = f"OrderIntent {intent.signal_id} has no legs."
            logger.error(msg)
            rejected_order = self._create_rejected_order(
                intent=intent,
                reason=msg,
                oms_received_at=oms_received_at,
                override_internal_id=override_internal_id,
            )
            return ExecutionResult(
                order=rejected_order,
                rms_result=rms_result,
                success=False,
                error_message=msg,
                orders=[rejected_order],
            )

        self._submitted_signals.add(duplicate_key)

        submitted: list[OMSOrder] = []
        first_error: str | None = None

        for index, _leg in enumerate(intent.legs):
            try:
                order = await self._submit_leg(
                    intent=intent,
                    rms_result=rms_result,
                    index=index,
                    oms_received_at=oms_received_at,
                    override_internal_id=override_internal_id,
                    limit_price=limit_price,
                    order_type=order_type,
                )
            except Exception as e:
                logger.exception("Failed to submit order for signal %s leg %d", intent.signal_id, index)
                first_error = first_error or str(e)
                continue
            submitted.append(order)
            if order.status in (OMSOrderStatus.REJECTED, OMSOrderStatus.ERROR):
                first_error = first_error or order.error_message

        if not submitted:
            rejected_order = self._create_rejected_order(
                intent=intent,
                reason=first_error or "No legs submitted",
                oms_received_at=oms_received_at,
                override_internal_id=override_internal_id,
            )
            return ExecutionResult(
                order=rejected_order,
                rms_result=rms_result,
                success=False,
                error_message=first_error or rejected_order.error_message,
                orders=[rejected_order],
            )

        success = first_error is None and all(
            o.status not in (OMSOrderStatus.REJECTED, OMSOrderStatus.ERROR) for o in submitted
        )
        return ExecutionResult(
            order=submitted[0],
            rms_result=rms_result,
            success=success,
            error_message=first_error,
            orders=submitted,
        )

    async def submit_one_leg(
        self,
        intent: OrderIntent,
        rms_result: RMSResult,
        index: int,
        *,
        oms_received_at: datetime | None = None,
        override_internal_id: str | None = None,
        limit_price: Decimal | None = None,
        order_type: str = "LIMIT",
        before_place: object | None = None,
    ) -> OMSOrder:
        """Submit a single intent leg. Does not wait for broker fill."""
        received = oms_received_at or datetime.now(UTC)
        duplicate_key = f"{intent.account_id}:{intent.signal_id}"
        if duplicate_key not in self._submitted_signals:
            if rms_result.outcome != RMSOutcome.PASS:
                raise ValueError("RMS must PASS before submitting a basket leg.")
            self._submitted_signals.add(duplicate_key)
        leg = intent.legs[index]
        logger.info(
            "OMS submit_one_leg handoff: signal_id=%s leg=%d/%d symbol=%s side=%s qty=%s order_type=%s",
            intent.signal_id,
            index + 1,
            len(intent.legs),
            leg.symbol,
            leg.side.value if hasattr(leg.side, "value") else leg.side,
            leg.quantity,
            order_type,
        )
        return await self._submit_leg(
            intent=intent,
            rms_result=rms_result,
            index=index,
            oms_received_at=received,
            override_internal_id=override_internal_id,
            limit_price=limit_price,
            order_type=order_type,
            before_place=before_place,
        )

    async def _submit_leg(
        self,
        *,
        intent: OrderIntent,
        rms_result: RMSResult,
        index: int,
        oms_received_at: datetime,
        override_internal_id: str | None,
        limit_price: Decimal | None,
        order_type: str,
        before_place: object | None = None,
    ) -> OMSOrder:
        if index < 0 or index >= len(intent.legs):
            raise IndexError(f"Leg index {index} out of range for intent {intent.signal_id}")
        leg = intent.legs[index]
        internal_order_id = self._leg_order_id(
            intent.signal_id,
            index,
            len(intent.legs),
            override_internal_id,
            account_id=intent.account_id,
        )
        price = self._leg_limit_price(leg, limit_price, len(intent.legs))
        try:
            resolved = leg.resolved or resolve_leg(
                symbol=leg.symbol,
                instrument_type=leg.instrument_type,
                market=intent.market or leg.exchange,
                currency=leg.currency,
                con_id=leg.con_id,
            )
        except InstrumentResolutionError as exc:
            order = OMSOrder(
                internal_order_id=internal_order_id,
                intent=intent,
                symbol=leg.symbol,
                side=leg.side,
                quantity=float(leg.quantity),
                limit_price=price,
                order_type=order_type,
                status=OMSOrderStatus.REJECTED,
                error_message=str(exc),
                parent_signal_id=intent.signal_id,
                leg_index=index,
            )
            order.timestamps.intent_created_at = intent.timestamp
            order.timestamps.rms_started_at = rms_result.timestamp
            order.timestamps.rms_completed_at = rms_result.timestamp
            order.timestamps.oms_received_at = oms_received_at
            self._orders[internal_order_id] = order
            return order
        order = OMSOrder(
            internal_order_id=internal_order_id,
            intent=replace(intent, legs=list(intent.legs)),
            symbol=leg.symbol,
            side=leg.side,
            quantity=float(leg.quantity),
            limit_price=price,
            order_type=order_type,
            status=OMSOrderStatus.PENDING,
            parent_signal_id=intent.signal_id,
            leg_index=index,
            resolved=resolved,
        )
        order.timestamps.intent_created_at = intent.timestamp
        order.timestamps.rms_started_at = rms_result.timestamp
        order.timestamps.rms_completed_at = rms_result.timestamp
        order.timestamps.oms_received_at = oms_received_at
        self._orders[internal_order_id] = order

        logger.info(
            "OMS created internal order %s for signal %s leg %d/%d: %s %s %s @ %s",
            internal_order_id,
            intent.signal_id,
            index + 1,
            len(intent.legs),
            order.side.value,
            order.quantity,
            order.symbol,
            price,
        )

        try:
            order = await self._adapter.submit_order(order, before_place=before_place)
            self._orders[internal_order_id] = order
        except Exception as e:
            logger.exception(
                "Failed to submit order %s (leg %d) to broker adapter",
                internal_order_id,
                index,
            )
            order.status = OMSOrderStatus.ERROR
            order.error_message = str(e)
        return order

    def _leg_order_id(
        self,
        signal_id: str,
        index: int,
        leg_count: int,
        override_internal_id: str | None,
        *,
        account_id: int | None = None,
    ) -> str:
        prefix = f"{account_id}-" if account_id is not None else ""
        if override_internal_id is not None:
            if leg_count == 1:
                return override_internal_id
            return f"{override_internal_id}-L{index}"
        if leg_count == 1:
            return f"ORD-{prefix}{signal_id}"
        return f"ORD-{prefix}{signal_id}-L{index}"

    def _leg_limit_price(
        self,
        leg: OrderLeg,
        limit_price: Decimal | None,
        leg_count: int,
    ) -> Decimal | None:
        if limit_price is not None and leg_count == 1:
            return limit_price
        return leg.price

    def _create_rejected_order(
        self,
        intent: OrderIntent,
        reason: str,
        oms_received_at: datetime,
        override_internal_id: str | None = None,
    ) -> OMSOrder:
        """Construct a REJECTED OMSOrder audit row when submission is declined.

        Uses the first leg only as a display snapshot; successful submission
        still iterates every intent.leg independently.
        """
        primary_leg = intent.legs[0] if intent.legs else None
        symbol = primary_leg.symbol if primary_leg else "UNKNOWN"
        side = primary_leg.side if primary_leg else None
        qty = float(primary_leg.quantity) if primary_leg else 0.0

        internal_id = override_internal_id or f"ORD-REJ-{intent.signal_id}"
        order = OMSOrder(
            internal_order_id=internal_id,
            intent=intent,
            symbol=symbol,
            side=side,  # type: ignore[arg-type]
            quantity=qty,
            status=OMSOrderStatus.REJECTED,
            error_message=reason,
            parent_signal_id=intent.signal_id,
            leg_index=0 if primary_leg else None,
        )
        order.timestamps.intent_created_at = intent.timestamp
        order.timestamps.oms_received_at = oms_received_at
        self._orders[internal_id] = order
        return order

    async def cancel_order(self, internal_order_id: str) -> OMSOrder:
        """Submit order cancellation request through IBKR adapter."""
        order = self.get_order(internal_order_id)
        if order is None:
            raise ValueError(f"Order {internal_order_id} not found.")

        return await self._adapter.cancel_order(order)
