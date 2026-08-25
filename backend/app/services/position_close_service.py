"""Single Pair Close Service.

Handles targeted closing of a single selected open position/pair for an account
without affecting other positions or activating the account-level Kill Switch.
"""

import asyncio
import logging
from decimal import Decimal
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.account import AccountModel
from app.db.repositories.event_repository import EventRepository
from app.db.repositories.position_repository import PositionRepository
from app.rms.models import (
    ExecutionIntentMode,
    OrderAction,
    OrderIntent,
    OrderLeg,
    RMSOutcome,
    RMSResult,
)
from app.rms.models import OrderSide as RMSOrderSide
from app.schemas.config_schemas import ClosePairResponse

logger = logging.getLogger(__name__)

# Global tracking of active in-flight close tasks by (account_id, trade_id) to prevent duplicate orders
_IN_FLIGHT_PAIR_CLOSES: dict[tuple[int, str], asyncio.Task[ClosePairResponse]] = {}


class SinglePairCloseService:
    """Service managing targeted single-pair position closing."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        order_manager: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._order_manager = order_manager

    async def close_pair(self, account_id: int, trade_id: str) -> ClosePairResponse:
        """Atomically execute close for a single open pair or return in-flight operation result."""
        key = (account_id, trade_id)

        # Idempotency check: if close task is currently running for this pair, await it
        if key in _IN_FLIGHT_PAIR_CLOSES:
            logger.info(
                "Duplicate close pair request received for account_id=%s trade_id=%s; returning active task",
                account_id,
                trade_id,
            )
            return await _IN_FLIGHT_PAIR_CLOSES[key]

        task = asyncio.create_task(self._do_close_pair(account_id, trade_id))
        _IN_FLIGHT_PAIR_CLOSES[key] = task
        try:
            return await task
        finally:
            _IN_FLIGHT_PAIR_CLOSES.pop(key, None)

    async def _do_close_pair(self, account_id: int, trade_id: str) -> ClosePairResponse:
        """Execute single position reduction and verify fills."""
        async with self._session_factory() as session:
            account = await session.get(AccountModel, account_id)
            if account is None:
                raise HTTPException(status_code=404, detail=f"Account {account_id} not found.")

            pos_repo = PositionRepository(session)
            pos = await pos_repo.get_open_by_trade_id(trade_id, account_id=account_id)
            if pos is None:
                # Check if position exists but is already closed
                existing = await pos_repo.get_by_trade_id(trade_id, account_id=account_id)
                if existing is not None:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Position '{trade_id}' for account {account_id} is already CLOSED.",
                    )
                raise HTTPException(
                    status_code=404,
                    detail=f"Open position '{trade_id}' not found for account {account_id}.",
                )

            ibkr_account = account.ibkr_account
            leg_a_symbol = pos.leg_a_symbol
            leg_b_symbol = pos.leg_b_symbol
            strategy_id = pos.strategy_id

            # Reverse leg construction
            legs: list[OrderLeg] = []
            if pos.leg_a_symbol and pos.leg_a_signed_qty is not None and abs(pos.leg_a_signed_qty) > 0:
                side = RMSOrderSide.SELL if pos.leg_a_signed_qty > 0 else RMSOrderSide.BUY
                qty = abs(pos.leg_a_signed_qty)
                legs.append(
                    OrderLeg(
                        symbol=pos.leg_a_symbol,
                        side=side,
                        quantity=qty,
                        price=Decimal(0),
                        contract_month="",
                        instrument_type=pos.leg_a_instrument_type or "STK",
                        leg_index=0,
                    )
                )

            if pos.leg_b_symbol and pos.leg_b_signed_qty is not None and abs(pos.leg_b_signed_qty) > 0:
                side = RMSOrderSide.SELL if pos.leg_b_signed_qty > 0 else RMSOrderSide.BUY
                qty = abs(pos.leg_b_signed_qty)
                legs.append(
                    OrderLeg(
                        symbol=pos.leg_b_symbol,
                        side=side,
                        quantity=qty,
                        price=Decimal(0),
                        contract_month="",
                        instrument_type=pos.leg_b_instrument_type or "STK",
                        leg_index=1,
                    )
                )

        if not legs:
            return ClosePairResponse(
                account_id=account_id,
                ibkr_account=ibkr_account,
                trade_id=trade_id,
                leg_a_symbol=leg_a_symbol,
                leg_b_symbol=leg_b_symbol,
                status="CLOSED",
                success=True,
                message="No active quantities to close.",
            )

        close_intent = OrderIntent(
            signal_id=f"CLOSEPAIR-{trade_id}-{uuid4().hex[:6]}",
            strategy_id=strategy_id,
            action=OrderAction.CLOSE,
            legs=legs,
            account_id=account_id,
            ibkr_account=ibkr_account,
            intent_mode=ExecutionIntentMode.EMERGENCY_FLATTEN,
        )

        baskets_coord = (
            getattr(self._order_manager, "_baskets", None) if self._order_manager else None
        )

        if baskets_coord is None:
            logger.error(
                "Single pair close failed: execution dependency (baskets coordinator) unavailable for account_id=%d trade_id=%s",
                account_id,
                trade_id,
            )
            return ClosePairResponse(
                account_id=account_id,
                ibkr_account=ibkr_account,
                trade_id=trade_id,
                leg_a_symbol=leg_a_symbol,
                leg_b_symbol=leg_b_symbol,
                status="FAILED",
                success=False,
                message="Execution dependency (baskets coordinator) is unavailable.",
            )

        if self._order_manager is not None:
            close_intent = await self._order_manager._resolve_instruments(close_intent)

        rms_pass = RMSResult(
            outcome=RMSOutcome.PASS,
            intent=close_intent,
            original_intent=close_intent,
            reason="CLOSE_SELECTED_PAIR",
        )

        try:
            res = await baskets_coord.execute(close_intent, rms_pass, order_type="MARKET")
            orders = getattr(res, "orders", [])

            from app.oms.models import OMSOrderStatus

            def _is_filled(o: Any) -> bool:
                st = getattr(o, "status", None)
                if st == OMSOrderStatus.FILLED or st == "FILLED":
                    return True
                return bool(hasattr(st, "value") and st.value == "FILLED")

            fill_orders = [o for o in orders if not getattr(o, "is_compensation", False)]
            is_fully_filled = bool(fill_orders) and all(_is_filled(o) for o in fill_orders)
            any_filled = any(_is_filled(o) for o in fill_orders) or any(
                (getattr(o, "filled_quantity", 0) or 0) > 0 for o in fill_orders
            )

            basket_success = getattr(res, "success", False) or is_fully_filled

            if basket_success and is_fully_filled:
                async with self._session_factory() as session, session.begin():
                    p_repo = PositionRepository(session)
                    p_row = await p_repo.get_open_by_trade_id(trade_id, account_id=account_id)
                    if p_row is not None:
                        try:
                            from app.services.model_blue.persistence import (
                                _commission_from_orders,
                                _exit_marks_from_orders,
                            )

                            exit_marks = _exit_marks_from_orders(fill_orders)
                            comm = _commission_from_orders(fill_orders)
                        except Exception:  # noqa: BLE001
                            exit_marks = {}
                            comm = Decimal(0)

                        await p_repo.close_trade(
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
                                "source": "CLOSE_PAIR",
                            },
                            idempotency_key=f"position_close:single_pair:{account_id}:{trade_id}",
                        )
                        logger.info(
                            "Single pair close persisted: account_id=%d trade_id=%s",
                            account_id,
                            trade_id,
                        )

                return ClosePairResponse(
                    account_id=account_id,
                    ibkr_account=ibkr_account,
                    trade_id=trade_id,
                    leg_a_symbol=leg_a_symbol,
                    leg_b_symbol=leg_b_symbol,
                    status="CLOSED",
                    success=True,
                    message="Pair successfully closed.",
                )
            else:
                status_str = "PARTIAL" if any_filled else "FAILED"
                logger.warning(
                    "Single pair close incomplete: account_id=%d trade_id=%s status=%s",
                    account_id,
                    trade_id,
                    status_str,
                )
                return ClosePairResponse(
                    account_id=account_id,
                    ibkr_account=ibkr_account,
                    trade_id=trade_id,
                    leg_a_symbol=leg_a_symbol,
                    leg_b_symbol=leg_b_symbol,
                    status=status_str,
                    success=False,
                    message=f"Pair close execution state: {status_str}.",
                )
        except Exception as exc:
            logger.exception("Single pair close execution error: trade_id=%s", trade_id)
            return ClosePairResponse(
                account_id=account_id,
                ibkr_account=ibkr_account,
                trade_id=trade_id,
                leg_a_symbol=leg_a_symbol,
                leg_b_symbol=leg_b_symbol,
                status="FAILED",
                success=False,
                message=f"Execution error: {exc}",
            )
