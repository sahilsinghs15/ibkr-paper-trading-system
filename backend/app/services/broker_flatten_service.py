"""Flatten a single IBKR broker snapshot line via the existing OMS path."""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.account import AccountModel
from app.db.repositories.broker_position_repository import BrokerPositionRepository
from app.oms.models import OMSOrderStatus
from app.rms.models import (
    ExecutionIntentMode,
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide as RMSOrderSide,
    RMSOutcome,
    RMSResult,
)
from app.schemas.reconcile_schemas import FlattenBrokerPositionResponse
from app.services.position_reconciler import QTY_EPSILON

logger = logging.getLogger(__name__)

_IN_FLIGHT_BROKER_FLATTENS: dict[tuple[str, int], asyncio.Task[FlattenBrokerPositionResponse]] = {}


class BrokerFlattenService:
    """Submit a MARKET reverse for one broker_positions snapshot line."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        order_manager: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._order_manager = order_manager

    async def flatten_line(
        self,
        *,
        ibkr_account: str,
        symbol: str,
        sec_type: str,
        con_id: int,
    ) -> FlattenBrokerPositionResponse:
        key = (ibkr_account, con_id)
        if key in _IN_FLIGHT_BROKER_FLATTENS:
            logger.info(
                "Duplicate broker flatten for ibkr_account=%s con_id=%s; awaiting active task",
                ibkr_account,
                con_id,
            )
            return await _IN_FLIGHT_BROKER_FLATTENS[key]

        task = asyncio.create_task(
            self._do_flatten_line(
                ibkr_account=ibkr_account,
                symbol=symbol,
                sec_type=sec_type,
                con_id=con_id,
            )
        )
        _IN_FLIGHT_BROKER_FLATTENS[key] = task
        try:
            return await task
        finally:
            _IN_FLIGHT_BROKER_FLATTENS.pop(key, None)

    async def _do_flatten_line(
        self,
        *,
        ibkr_account: str,
        symbol: str,
        sec_type: str,
        con_id: int,
    ) -> FlattenBrokerPositionResponse:
        async with self._session_factory() as session:
            repo = BrokerPositionRepository(session)
            snapshot = await repo.get_snapshot_line(ibkr_account=ibkr_account, con_id=con_id)
            if snapshot is None:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"Broker snapshot line not found for account {ibkr_account!r} "
                        f"con_id={con_id}."
                    ),
                )

            norm_symbol = symbol.strip().upper()
            norm_sec_type = sec_type.strip().upper()
            if snapshot.symbol.strip().upper() != norm_symbol:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Symbol mismatch: request {norm_symbol!r} vs snapshot "
                        f"{snapshot.symbol.strip().upper()!r}."
                    ),
                )
            if snapshot.sec_type.strip().upper() != norm_sec_type:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Sec type mismatch: request {norm_sec_type!r} vs snapshot "
                        f"{snapshot.sec_type.strip().upper()!r}."
                    ),
                )

            signed_qty = float(snapshot.signed_qty)
            if abs(signed_qty) <= QTY_EPSILON:
                raise HTTPException(
                    status_code=400,
                    detail="Broker snapshot quantity is zero; nothing to flatten.",
                )

            account_id = snapshot.account_id
            if account_id is None:
                account_row = (
                    await session.execute(
                        select(AccountModel.id).where(AccountModel.ibkr_account == ibkr_account)
                    )
                ).scalar_one_or_none()
                account_id = account_row

            close_side = RMSOrderSide.SELL if signed_qty > 0 else RMSOrderSide.BUY
            close_qty = abs(signed_qty)
            side_label = close_side.value
            snapshot_exchange = snapshot.exchange or None
            snapshot_currency = snapshot.currency or None

        baskets_coord = (
            getattr(self._order_manager, "_baskets", None) if self._order_manager else None
        )
        if baskets_coord is None:
            raise HTTPException(
                status_code=503,
                detail="Execution dependency (baskets coordinator) is unavailable.",
            )

        flatten_intent = OrderIntent(
            signal_id=f"RECON-FLAT-{con_id}-{uuid4().hex[:6]}",
            strategy_id="reconcile_flatten",
            action=OrderAction.CLOSE,
            legs=[
                OrderLeg(
                    symbol=norm_symbol,
                    side=close_side,
                    quantity=close_qty,
                    price=Decimal(0),
                    contract_month="",
                    con_id=con_id,
                    instrument_type=norm_sec_type,
                    exchange=snapshot_exchange,
                    currency=snapshot_currency,
                    leg_index=0,
                )
            ],
            account_id=account_id,
            ibkr_account=ibkr_account,
            intent_mode=ExecutionIntentMode.EMERGENCY_FLATTEN,
        )

        if self._order_manager is not None:
            flatten_intent = await self._order_manager._resolve_instruments(flatten_intent)

        rms_pass = RMSResult(
            outcome=RMSOutcome.PASS,
            intent=flatten_intent,
            original_intent=flatten_intent,
            reason="RECONCILE_BROKER_FLATTEN",
        )

        try:
            res = await baskets_coord.execute(flatten_intent, rms_pass, order_type="MARKET")
            orders = getattr(res, "orders", [])

            def _is_filled(order: Any) -> bool:
                st = getattr(order, "status", None)
                if st == OMSOrderStatus.FILLED or st == "FILLED":
                    return True
                return bool(hasattr(st, "value") and st.value == "FILLED")

            fill_orders = [o for o in orders if not getattr(o, "is_compensation", False)]
            is_fully_filled = bool(fill_orders) and all(_is_filled(o) for o in fill_orders)
            any_filled = any(_is_filled(o) for o in fill_orders) or any(
                (getattr(o, "filled_quantity", 0) or 0) > 0 for o in fill_orders
            )

            if is_fully_filled:
                status = "FLAT"
                success = True
                message = "Broker line flattened successfully."
            elif any_filled:
                status = "PARTIAL"
                success = False
                message = "Broker flatten partially filled."
            else:
                status = "FAILED"
                success = False
                message = "Broker flatten did not fill."

            logger.info(
                "Reconcile broker flatten ibkr_account=%s con_id=%s symbol=%s status=%s",
                ibkr_account,
                con_id,
                norm_symbol,
                status,
            )
            return FlattenBrokerPositionResponse(
                ibkr_account=ibkr_account,
                account_id=account_id,
                symbol=norm_symbol,
                sec_type=norm_sec_type,
                con_id=con_id,
                side=side_label,
                quantity=close_qty,
                status=status,
                success=success,
                message=message,
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception(
                "Reconcile broker flatten failed ibkr_account=%s con_id=%s",
                ibkr_account,
                con_id,
            )
            return FlattenBrokerPositionResponse(
                ibkr_account=ibkr_account,
                account_id=account_id,
                symbol=norm_symbol,
                sec_type=norm_sec_type,
                con_id=con_id,
                side=side_label,
                quantity=close_qty,
                status="FAILED",
                success=False,
                message=f"Execution error: {exc}",
            )
