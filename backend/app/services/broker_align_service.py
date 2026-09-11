"""Align one IBKR broker net line to the OPEN Model Blue ledger via MARKET."""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.identifiers import normalize_account
from app.db.models.account import AccountModel
from app.db.models.instrument import InstrumentModel
from app.db.models.manual_order import ManualPositionModel
from app.db.repositories.broker_position_repository import BrokerPositionRepository
from app.db.repositories.position_repository import PositionRepository
from app.oms.models import OMSOrderStatus
from app.rms.models import (
    ExecutionIntentMode,
    OrderAction,
    OrderIntent,
    OrderLeg,
    RMSOutcome,
    RMSResult,
)
from app.rms.models import (
    OrderSide as RMSOrderSide,
)
from app.schemas.reconcile_schemas import FlattenBrokerPositionResponse
from app.services.position_reconciler import (
    QTY_EPSILON,
    fetch_in_flight_accounts,
    ledger_net_qty_for_symbol,
)

logger = logging.getLogger(__name__)

_IN_FLIGHT_BROKER_ALIGNS: dict[tuple[str, int], asyncio.Task[FlattenBrokerPositionResponse]] = {}


def _align_action(current: float, target: float) -> OrderAction:
    """Choose OPEN vs CLOSE for a single-leg align trade."""
    if abs(current) <= QTY_EPSILON:
        return OrderAction.OPEN
    if abs(target) <= QTY_EPSILON:
        return OrderAction.CLOSE
    if (current > 0 and target > 0) or (current < 0 and target < 0):
        if abs(target) > abs(current) + QTY_EPSILON:
            return OrderAction.OPEN
        return OrderAction.CLOSE
    return OrderAction.CLOSE


def _find_instrument(
    instruments: list[InstrumentModel],
    *,
    symbol: str,
    sec_type: str,
    con_id: int,
) -> InstrumentModel | None:
    norm_symbol = symbol.strip().upper()
    norm_sec_type = sec_type.strip().upper()
    for inst in instruments:
        if (
            inst.symbol.strip().upper() == norm_symbol
            and inst.sec_type.strip().upper() == norm_sec_type
            and int(inst.trade_conid) == con_id
        ):
            return inst
    for inst in instruments:
        if (
            inst.symbol.strip().upper() == norm_symbol
            and inst.sec_type.strip().upper() == norm_sec_type
        ):
            return inst
    return None


class BrokerAlignService:
    """Submit a MARKET trade so broker net qty matches the OPEN ledger net."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        order_manager: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._order_manager = order_manager

    async def align_line(
        self,
        *,
        ibkr_account: str,
        symbol: str,
        sec_type: str,
        con_id: int,
    ) -> FlattenBrokerPositionResponse:
        key = (ibkr_account.strip().upper(), con_id)
        if key in _IN_FLIGHT_BROKER_ALIGNS:
            logger.info(
                "Duplicate broker align for ibkr_account=%s con_id=%s; awaiting active task",
                ibkr_account,
                con_id,
            )
            return await _IN_FLIGHT_BROKER_ALIGNS[key]

        task = asyncio.create_task(
            self._do_align_line(
                ibkr_account=ibkr_account,
                symbol=symbol,
                sec_type=sec_type,
                con_id=con_id,
            )
        )
        _IN_FLIGHT_BROKER_ALIGNS[key] = task
        try:
            return await task
        finally:
            _IN_FLIGHT_BROKER_ALIGNS.pop(key, None)

    async def _do_align_line(
        self,
        *,
        ibkr_account: str,
        symbol: str,
        sec_type: str,
        con_id: int,
    ) -> FlattenBrokerPositionResponse:
        norm_symbol = symbol.strip().upper()
        norm_sec_type = sec_type.strip().upper()
        norm_ibkr = normalize_account(ibkr_account)
        raw_ibkr = ibkr_account.strip()

        async with self._session_factory() as session:
            repo = BrokerPositionRepository(session)
            snapshot = await repo.get_snapshot_line(ibkr_account=raw_ibkr, con_id=con_id)
            if snapshot is None and raw_ibkr.upper() != raw_ibkr:
                snapshot = await repo.get_snapshot_line(ibkr_account=norm_ibkr, con_id=con_id)

            account_id = snapshot.account_id if snapshot is not None else None
            if account_id is None:
                account_id = (
                    await session.execute(
                        select(AccountModel.id).where(
                            AccountModel.ibkr_account == ibkr_account.strip()
                        )
                    )
                ).scalar_one_or_none()
            if account_id is None:
                account_id = (
                    await session.execute(
                        select(AccountModel.id).where(AccountModel.ibkr_account == norm_ibkr)
                    )
                ).scalar_one_or_none()
            if account_id is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Account not found for IBKR account {norm_ibkr!r}.",
                )

            in_flight_accounts = await fetch_in_flight_accounts(session)
            if account_id in in_flight_accounts:
                raise HTTPException(
                    status_code=409,
                    detail="Account has in-flight execution; align is blocked.",
                )

            open_rows = await PositionRepository(session).list_open()
            manual_open_rows = list(
                (
                    await session.execute(
                        select(ManualPositionModel).where(
                            ManualPositionModel.status == "OPEN",
                            ManualPositionModel.account_id == account_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            instruments = list(
                (await session.execute(select(InstrumentModel))).scalars().all()
            )
            ledger_net = ledger_net_qty_for_symbol(
                open_rows,
                instruments,
                account_id=account_id,
                symbol=norm_symbol,
                sec_type=norm_sec_type,
                manual_open_rows=manual_open_rows,
            )
            target = ledger_net if ledger_net is not None else 0.0

            current = float(snapshot.signed_qty) if snapshot is not None else 0.0

            if snapshot is not None:
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

            delta = target - current
            if abs(delta) <= QTY_EPSILON:
                raise HTTPException(
                    status_code=400,
                    detail="Broker quantity already matches ledger; nothing to align.",
                )

            trade_side = RMSOrderSide.BUY if delta > 0 else RMSOrderSide.SELL
            trade_qty = abs(delta)
            side_label = trade_side.value
            order_action = _align_action(current, target)

            if snapshot is not None:
                resolved_con_id = int(snapshot.con_id)
                snapshot_exchange = snapshot.exchange or None
                snapshot_currency = snapshot.currency or None
            else:
                if abs(target) <= QTY_EPSILON:
                    raise HTTPException(
                        status_code=400,
                        detail="No broker snapshot line and ledger net is zero.",
                    )
                instrument = _find_instrument(
                    instruments,
                    symbol=norm_symbol,
                    sec_type=norm_sec_type,
                    con_id=con_id,
                )
                if instrument is None:
                    resolved_con_id = int(con_id)
                    snapshot_exchange = "SMART"
                    snapshot_currency = "USD"
                else:
                    resolved_con_id = int(instrument.trade_conid)
                    if resolved_con_id != con_id:
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                f"con_id mismatch: request {con_id} vs instrument "
                                f"trade_conid {resolved_con_id}."
                            ),
                        )
                    snapshot_exchange = instrument.exchange or None
                    snapshot_currency = instrument.currency or None

            from app.services import flatten_inflight

            flatten_keys: list = [flatten_inflight.broker_key(norm_ibkr, resolved_con_id)]
            for pos in open_rows:
                if pos.account_id != account_id:
                    continue
                symbols = {
                    (pos.leg_a_symbol or "").strip().upper(),
                    (pos.leg_b_symbol or "").strip().upper(),
                }
                if norm_symbol in symbols:
                    flatten_keys.append(
                        flatten_inflight.ledger_key(account_id, pos.trade_id)
                    )

        from app.services import flatten_inflight as _fi

        if not await _fi.try_acquire_many(flatten_keys):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Align already in progress for ibkr_account={norm_ibkr} "
                    f"con_id={resolved_con_id}."
                ),
            )
        try:
            return await self._submit_align_line(
                ibkr_account=norm_ibkr,
                account_id=account_id,
                norm_symbol=norm_symbol,
                norm_sec_type=norm_sec_type,
                con_id=resolved_con_id,
                trade_side=trade_side,
                trade_qty=trade_qty,
                side_label=side_label,
                order_action=order_action,
                snapshot_exchange=snapshot_exchange,
                snapshot_currency=snapshot_currency,
            )
        finally:
            await _fi.release_many(flatten_keys)

    async def _submit_align_line(
        self,
        *,
        ibkr_account: str,
        account_id: int,
        norm_symbol: str,
        norm_sec_type: str,
        con_id: int,
        trade_side: RMSOrderSide,
        trade_qty: float,
        side_label: str,
        order_action: OrderAction,
        snapshot_exchange: str | None,
        snapshot_currency: str | None,
    ) -> FlattenBrokerPositionResponse:
        baskets_coord = (
            getattr(self._order_manager, "_baskets", None) if self._order_manager else None
        )
        if baskets_coord is None:
            raise HTTPException(
                status_code=503,
                detail="Execution dependency (baskets coordinator) is unavailable.",
            )

        align_intent = OrderIntent(
            signal_id=f"RECON-ALIGN-{con_id}",
            strategy_id="reconcile_align",
            action=order_action,
            legs=[
                OrderLeg(
                    symbol=norm_symbol,
                    side=trade_side,
                    quantity=trade_qty,
                    price=Decimal(0),
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
            align_intent = await self._order_manager._resolve_instruments(align_intent)

        rms_pass = RMSResult(
            outcome=RMSOutcome.PASS,
            intent=align_intent,
            original_intent=align_intent,
            reason="RECONCILE_BROKER_ALIGN",
        )

        try:
            res = await baskets_coord.execute(align_intent, rms_pass, order_type="MARKET")
            orders = getattr(res, "orders", [])

            def _is_filled(order: Any) -> bool:
                st = getattr(order, "status", None)
                if st == OMSOrderStatus.FILLED or st == "FILLED":
                    return True
                return bool(hasattr(st, "value") and st.value == "FILLED")  # type: ignore[union-attr]

            fill_orders = [o for o in orders if not getattr(o, "is_compensation", False)]
            is_fully_filled = bool(fill_orders) and all(_is_filled(o) for o in fill_orders)
            any_filled = any(_is_filled(o) for o in fill_orders) or any(
                (getattr(o, "filled_quantity", 0) or 0) > 0 for o in fill_orders
            )

            if is_fully_filled:
                status = "ALIGNED"
                success = True
                message = "Broker line aligned to ledger successfully."
            elif any_filled:
                status = "PARTIAL"
                success = False
                message = "Broker align partially filled."
            else:
                status = "FAILED"
                success = False
                message = "Broker align did not fill."

            logger.info(
                "Reconcile broker align ibkr_account=%s con_id=%s symbol=%s status=%s",
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
                quantity=trade_qty,
                status=status,
                success=success,
                message=message,
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception(
                "Reconcile broker align failed ibkr_account=%s con_id=%s",
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
                quantity=trade_qty,
                status="FAILED",
                success=False,
                message=f"Execution error: {exc}",
            )
