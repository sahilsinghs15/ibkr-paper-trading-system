"""OrderManager — application facade orchestrating Signal -> OrderIntent -> RMS -> OMS."""

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from app.models.signal import Signal, SignalType
from app.oms.models import OMSOrder
from app.oms.oms_service import OMSService
from app.rms.engine import RMSEngine
from app.rms.models import (
    OrderAction,
    OrderIntent,
    OrderLeg,
    RMSContext,
    RMSOutcome,
    StrategyConfig,
)
from app.rms.models import (
    OrderSide as RMSOrderSide,
)

logger = logging.getLogger(__name__)


class OrderManager:
    """Application-level order execution facade.

    Translates strategy Signals into OrderIntents, evaluates RMS rules,
    and submits approved orders to the OMS.
    """

    def __init__(
        self,
        oms: OMSService | None = None,
        symbol: str = "RELIANCE",
        quantity: int = 1,
        order_type: str = "MARKET",
        price: Decimal = Decimal("100.00"),
        strategy_id: str = "MODEL_BLUE",
        rms_engine: RMSEngine | None = None,
        rms_context: RMSContext | None = None,
    ) -> None:
        self._oms = oms
        self._symbol = symbol
        self._quantity = quantity
        self._order_type = order_type
        self._price = price
        self._strategy_id = strategy_id

        self._rms_engine = rms_engine or RMSEngine()
        self._rms_context = rms_context or RMSContext(
            strategy_configs={
                strategy_id: StrategyConfig(
                    strategy_id=strategy_id,
                    max_open_positions=100,
                    money_limit_per_symbol=Decimal(10_000_000),
                )
            }
        )

    async def process_signal(self, signal: Signal) -> OMSOrder | None:
        """Process a trading signal through RMS evaluation and submit to OMS.

        Args:
            signal: A Signal produced by an external source.

        Returns:
            The resulting OMSOrder object for BUY/SELL signals, or None for HOLD.
        """
        if signal.signal_type == SignalType.HOLD:
            logger.info("HOLD signal received — no order submitted")
            return None

        strat_id = signal.strategy_id or self._strategy_id
        sig_id = signal.signal_id or f"SIG-{uuid.uuid4().hex[:12].upper()}"
        target_symbol = signal.symbol or self._symbol
        target_price = signal.price if signal.price is not None else self._price

        action_val = str(signal.action or "OPEN").upper()
        order_action = OrderAction.CLOSE if action_val == "CLOSE" else OrderAction.OPEN

        if signal.side:
            side_str = str(signal.side).upper()
            rms_side = RMSOrderSide.SELL if side_str in ("SELL", "SHORT") else RMSOrderSide.BUY
        else:
            rms_side = RMSOrderSide.BUY if signal.signal_type == SignalType.BUY else RMSOrderSide.SELL

        logger.info(
            "%s signal received — submitting order: symbol=%s qty=%s type=%s price=%s action=%s",
            signal.signal_type.value,
            target_symbol,
            str(self._quantity),
            self._order_type,
            str(target_price),
            order_action.value,
        )

        intent = OrderIntent(
            signal_id=sig_id,
            strategy_id=strat_id,
            action=order_action,
            legs=[
                OrderLeg(
                    symbol=target_symbol,
                    side=rms_side,
                    quantity=int(self._quantity) if isinstance(self._quantity, int) else 1,
                    price=target_price,
                    contract_month="2026-09",
                )
            ],
            timestamp=signal.timestamp or datetime.now(UTC),
        )

        # Ensure strategy config exists in RMS context
        if strat_id not in self._rms_context.strategy_configs:
            self._rms_context.strategy_configs[strat_id] = StrategyConfig(
                strategy_id=strat_id,
                max_open_positions=100,
                money_limit_per_symbol=Decimal(10_000_000),
            )

        # 1. Risk evaluation
        rms_result = self._rms_engine.evaluate(intent, self._rms_context)
        if rms_result.outcome != RMSOutcome.PASS:
            msg = f"RMS check {rms_result.check_number} rejected intent: {rms_result.reason}"
            logger.warning(msg)
            raise ValueError(msg)

        # 2. OMS submission
        if self._oms is not None:
            exec_res = await self._oms.submit_intent(
                intent=intent,
                rms_result=rms_result,
                limit_price=target_price,
                order_type=self._order_type,
            )
            if not exec_res.success:
                raise RuntimeError(f"OMS submission failed: {exec_res.error_message}")
            return exec_res.order

        raise RuntimeError("No OMSService configured on OrderManager.")

