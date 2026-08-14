"""Order Management System (OMS) service orchestrating order lifecycle and broker execution."""

import logging
from datetime import UTC, datetime
from decimal import Decimal

from app.oms.ibkr_adapter import IBKRExecutionAdapter
from app.oms.models import ExecutionResult, OMSOrder, OMSOrderStatus
from app.rms.models import OrderIntent, OrderLeg, RMSOutcome, RMSResult

logger = logging.getLogger(__name__)


class OMSService:
    """Minimal Order Management System service.

    Responsible for:
      1. Validating RMS PASS status on incoming OrderIntents.
      2. Constructing internal OMSOrder representations.
      3. Submitting orders through an injected IBKRExecutionAdapter.
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

        Args:
            intent: The target OrderIntent.
            rms_result: The evaluation result returned by RMS.
            override_internal_id: Optional explicit internal order ID (e.g. for testing).
            limit_price: Optional limit price override; if None, derived from order leg.
            order_type: Order type string ("LIMIT" or "MARKET").

        Returns:
            ExecutionResult containing OMSOrder state and RMS evaluation details.

        Raises:
            ValueError: If the OrderIntent has not passed RMS evaluation or duplicate signal ID.
        """
        oms_received_at = datetime.now(UTC)

        # 1. Reject submission if RMS outcome is not PASS
        if rms_result.outcome != RMSOutcome.PASS:
            logger.warning(
                "OMS rejected intent %s: RMS outcome is %s (reason: %s)",
                intent.signal_id,
                rms_result.outcome.value,
                rms_result.reason,
            )
            # Construct a rejected internal order for auditing
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
            )

        # 2. Check duplicate submission attempt by signal_id
        if intent.signal_id in self._submitted_signals:
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
            )

        # Mark signal_id as submitted
        self._submitted_signals.add(intent.signal_id)

        # 3. Derive order parameters from intent leg
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
            )

        primary_leg: OrderLeg = intent.legs[0]
        internal_order_id = override_internal_id or f"ORD-{intent.signal_id}"
        price = limit_price if limit_price is not None else primary_leg.price

        # 4. Construct internal OMSOrder representation
        order = OMSOrder(
            internal_order_id=internal_order_id,
            intent=intent,
            symbol=primary_leg.symbol,
            side=primary_leg.side,
            quantity=primary_leg.quantity,
            limit_price=price,
            order_type=order_type,
            status=OMSOrderStatus.PENDING,
        )

        # Record timestamps
        order.timestamps.intent_created_at = intent.timestamp
        order.timestamps.rms_started_at = rms_result.timestamp  # Best available reference
        order.timestamps.rms_completed_at = rms_result.timestamp
        order.timestamps.oms_received_at = oms_received_at

        self._orders[internal_order_id] = order

        logger.info(
            "OMS created internal order %s for signal %s: %s %d %s @ %s",
            internal_order_id,
            intent.signal_id,
            order.side.value,
            order.quantity,
            order.symbol,
            price,
        )

        # 5. Submit through IBKR Execution Adapter
        try:
            order = await self._adapter.submit_order(order)
            return ExecutionResult(
                order=order,
                rms_result=rms_result,
                success=order.status not in (OMSOrderStatus.REJECTED, OMSOrderStatus.ERROR),
            )
        except Exception as e:
            logger.exception("Failed to submit order %s to broker adapter", internal_order_id)
            order.status = OMSOrderStatus.ERROR
            order.error_message = str(e)
            return ExecutionResult(
                order=order,
                rms_result=rms_result,
                success=False,
                error_message=str(e),
            )

    def _create_rejected_order(
        self,
        intent: OrderIntent,
        reason: str,
        oms_received_at: datetime,
        override_internal_id: str | None = None,
    ) -> OMSOrder:
        """Construct a REJECTED OMSOrder record when submission is declined."""
        primary_leg = intent.legs[0] if intent.legs else None
        symbol = primary_leg.symbol if primary_leg else "UNKNOWN"
        side = primary_leg.side if primary_leg else None
        qty = primary_leg.quantity if primary_leg else 0

        internal_id = override_internal_id or f"ORD-REJ-{intent.signal_id}"
        order = OMSOrder(
            internal_order_id=internal_id,
            intent=intent,
            symbol=symbol,
            side=side,  # type: ignore[arg-type]
            quantity=qty,
            status=OMSOrderStatus.REJECTED,
            error_message=reason,
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
