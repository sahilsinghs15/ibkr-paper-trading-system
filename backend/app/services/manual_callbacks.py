"""Inbound IBKR execution and order status callbacks listener for Manual Trading (M1-C).

Multiplexes callbacks from the single TWSClient background reader thread,
deduplicates execution fills, applies atomic position ledger mutations, and
calculates realized P&L.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.broker.ibkr.executions import (
    BrokerExecutionLine,
    normalize_execution_side,
    parse_ibkr_execution_time,
)
from app.db.models.account import AccountModel
from app.db.models.manual_order import ManualOrderModel
from app.db.repositories.manual_repository import (
    ManualAuditRepository,
    ManualExecutionRepository,
    ManualOrderRepository,
    ManualPositionRepository,
)
from app.db.repositories.trade_execution_repository import TradeExecutionRepository

logger = logging.getLogger(__name__)


def _map_ib_order_status(raw_status: str) -> str:
    """Map raw IBKR order status string to canonical manual order status."""
    norm = raw_status.strip().upper().replace(" ", "")
    if norm in ("SUBMITTED", "PENDINGSUBMIT", "PRESUBMITTED", "APIPENDING"):
        return "SUBMITTED"
    if norm == "PARTIALLYFILLED":
        return "PARTIALLY_FILLED"
    if norm == "FILLED":
        return "FILLED"
    if norm in ("CANCELLED", "APICANCELLED"):
        return "CANCELLED"
    if norm in ("INACTIVE", "REJECTED"):
        return "REJECTED"
    return "SUBMITTED"


class ManualExecutionListener:
    """Listens to callbacks on the single TWSClient and processes manual orders."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: Any = None,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._client = client
        self._loop = loop
        self._pending_commissions: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind active event loop for scheduling async coroutines from background thread."""
        self._loop = loop

    def _dispatch(self, coro: Any) -> None:
        """Schedule coroutine thread-safely onto the event loop."""
        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

        if loop is not None and loop.is_running():
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None

            if running is loop:
                asyncio.create_task(coro)
            else:
                asyncio.run_coroutine_threadsafe(coro, loop)
        else:
            # Fallback: create task if in async context
            try:
                asyncio.create_task(coro)
            except RuntimeError:
                logger.error("No active event loop available to dispatch manual callback")

    # ── TWSClient Listener Protocol Callbacks ────────────────────────

    def on_order_status(
        self,
        orderId: int,
        status: str,
        filled: float,
        remaining: float,
        avgFillPrice: float,
        permId: int,
        parentId: int,
        lastFillPrice: float,
        clientId: int,
        whyHeld: str,
        mktCapPrice: float,
    ) -> None:
        """Handle orderStatus callback from TWSClient."""
        self._dispatch(
            self.handle_order_status(
                order_id=orderId,
                status=status,
                perm_id=permId,
            )
        )

    def on_open_order(
        self,
        orderId: int,
        contract: Any,
        order: Any,
        orderState: Any,
    ) -> None:
        """Handle openOrder callback from TWSClient."""
        order_ref = getattr(order, "orderRef", None) or ""
        perm_id = getattr(order, "permId", 0) or 0
        raw_status = getattr(orderState, "status", "") or ""
        self._dispatch(
            self.handle_open_order(
                order_id=orderId,
                order_ref=str(order_ref),
                perm_id=int(perm_id),
                raw_status=str(raw_status),
            )
        )

    def on_exec_details(self, reqId: int, contract: Any, execution: Any) -> None:
        """Handle execDetails callback from TWSClient."""
        self._dispatch(self.handle_exec_details(contract=contract, execution=execution))

    def on_commission_report(self, commissionReport: Any) -> None:
        """Handle commissionReport callback from TWSClient."""
        self._dispatch(self.handle_commission_report(commission_report=commissionReport))

    def on_error(self, reqId: int, errorCode: int, errorString: str) -> None:
        """Handle error callback from TWSClient."""
        logger.warning(
            "ManualExecutionListener on_error: reqId=%d, code=%d, msg=%s",
            reqId,
            errorCode,
            errorString,
        )

    def on_connection_closed(self) -> None:
        """Handle connection drop from TWSClient."""
        logger.warning("ManualExecutionListener: TWS connection closed")

    # ── Async Callback Handlers ──────────────────────────────────────

    async def handle_order_status(
        self,
        *,
        order_id: int,
        status: str,
        perm_id: int = 0,
    ) -> ManualOrderModel | None:
        """Correlate orderStatus and update manual order lifecycle status."""
        async with self._session_factory() as session, session.begin():
            order_repo = ManualOrderRepository(session)
            audit_repo = ManualAuditRepository(session)

            order = await order_repo.get_by_broker_order_id(str(order_id))
            if order is None and perm_id > 0:
                order = await order_repo.get_by_perm_id(perm_id)

            if order is None:
                return None

            # Capture perm_id if newly learned
            current_perm = order.perm_id
            new_perm = perm_id if (perm_id > 0 and not current_perm) else None

            mapped_status = _map_ib_order_status(status)

            # Invariant: Terminal states cannot be regressed by broker callbacks.
            # FILLED is permanently terminal - late CANCELLED, REJECTED, SUBMITTED callbacks must NEVER overwrite it.
            if order.status == "FILLED":
                logger.info(
                    "Dropping broker status callback %s for order %s because it is already FILLED",
                    status,
                    order.internal_order_id,
                )
                if new_perm is not None:
                    await order_repo.update_status(
                        order.id,
                        status="FILLED",
                        perm_id=new_perm,
                    )
                return order

            # CANCELLED, REJECTED, and ERROR are also terminal.
            if order.status in ("CANCELLED", "REJECTED", "ERROR"):
                logger.info(
                    "Dropping broker status callback %s for order %s because it is already in terminal state %s",
                    status,
                    order.internal_order_id,
                    order.status,
                )
                if new_perm is not None:
                    await order_repo.update_status(
                        order.id,
                        status=order.status,
                        perm_id=new_perm,
                    )
                return order

            # PARTIALLY_FILLED cannot regress to SUBMITTED
            if order.status == "PARTIALLY_FILLED" and mapped_status == "SUBMITTED":
                mapped_status = "PARTIALLY_FILLED"

            now = datetime.now(UTC)
            completed_at = now if mapped_status in ("FILLED", "CANCELLED", "REJECTED") else None

            updated = await order_repo.update_status(
                order.id,
                status=mapped_status,
                perm_id=new_perm,
                completed_at=completed_at,
            )

            await audit_repo.record_event(
                account_id=order.account_id,
                action="MANUAL_ORDER_STATUS_UPDATED",
                payload={
                    "internal_order_id": order.internal_order_id,
                    "broker_order_id": order.broker_order_id,
                    "perm_id": order.perm_id,
                    "raw_status": status,
                    "mapped_status": mapped_status,
                },
            )
            return updated

    async def handle_open_order(
        self,
        *,
        order_id: int,
        order_ref: str,
        perm_id: int,
        raw_status: str,
    ) -> ManualOrderModel | None:
        """Correlate openOrder and update manual order."""
        async with self._session_factory() as session, session.begin():
            order_repo = ManualOrderRepository(session)
            order = None

            if order_ref:
                order = await order_repo.get_by_internal_id(order_ref)
            if order is None and order_id > 0:
                order = await order_repo.get_by_broker_order_id(str(order_id))
            if order is None and perm_id > 0:
                order = await order_repo.get_by_perm_id(perm_id)

            if order is None:
                return None

            # Associate broker_order_id or perm_id if missing
            b_id = str(order_id) if not order.broker_order_id and order_id > 0 else None
            p_id = perm_id if not order.perm_id and perm_id > 0 else None

            # Invariant: openOrder callback must NEVER resurrect an order in a terminal state
            if order.status in ("FILLED", "CANCELLED", "REJECTED", "ERROR"):
                logger.info(
                    "openOrder callback received for terminal order %s (status=%s). Updating metadata only.",
                    order.internal_order_id,
                    order.status,
                )
                if b_id is not None or p_id is not None:
                    return await order_repo.update_status(
                        order.id,
                        status=order.status,
                        broker_order_id=b_id,
                        perm_id=p_id,
                    )
                return order

            mapped_status = _map_ib_order_status(raw_status) if raw_status else order.status

            # Prevent PARTIALLY_FILLED from regressing to SUBMITTED
            if order.status == "PARTIALLY_FILLED" and mapped_status == "SUBMITTED":
                mapped_status = "PARTIALLY_FILLED"

            return await order_repo.update_status(
                order.id,
                status=mapped_status,
                broker_order_id=b_id,
                perm_id=p_id,
            )

    async def handle_exec_details(
        self,
        *,
        contract: Any,
        execution: Any,
    ) -> tuple[ManualOrderModel | None, bool]:
        """Correlate execDetails, persist execution, mutate position exactly once.

        Returns (manual_order, is_new_execution).
        """
        raw_exec_id = str(getattr(execution, "execId", "") or "").strip()
        if not raw_exec_id:
            return None, False

        tws_order_id = getattr(execution, "orderId", None)
        perm_id = getattr(execution, "permId", None)
        order_ref = str(getattr(execution, "orderRef", "") or "").strip()
        acct_number = str(getattr(execution, "acctNumber", "") or "").strip().upper()

        shares = Decimal(str(getattr(execution, "shares", 0) or 0))
        price = Decimal(str(getattr(execution, "price", 0) or 0))
        side = normalize_execution_side(getattr(execution, "side", None))

        exec_time_raw = getattr(execution, "time", None)
        executed_at_iso = parse_ibkr_execution_time(exec_time_raw)
        try:
            executed_at = datetime.fromisoformat(executed_at_iso)
        except (ValueError, TypeError):
            executed_at = datetime.now(UTC)

        # Check for buffered commission that arrived out-of-order
        buffered_commission: Decimal | None = None
        buffered_currency: str | None = None
        with self._lock:
            pending = self._pending_commissions.pop(raw_exec_id, None)
            if pending:
                buffered_commission = pending.get("commission")
                buffered_currency = pending.get("currency")

        async with self._session_factory() as session, session.begin():
            order_repo = ManualOrderRepository(session)
            exec_repo = ManualExecutionRepository(session)
            pos_repo = ManualPositionRepository(session)
            audit_repo = ManualAuditRepository(session)
            trade_exec_repo = TradeExecutionRepository(session)

            account_id: int | None = None
            if acct_number:
                acc_id_row = (
                    await session.execute(
                        select(AccountModel.id).where(AccountModel.ibkr_account == acct_number)
                    )
                ).scalar_one_or_none()
                if acc_id_row is not None:
                    account_id = acc_id_row

            # 1. Correlate to manual order
            order: ManualOrderModel | None = None
            if tws_order_id is not None and int(tws_order_id) > 0:
                order = await order_repo.get_by_broker_order_id(str(tws_order_id), account_id=account_id)
            if order is None and order_ref:
                order = await order_repo.get_by_internal_id(order_ref)
            if order is None and perm_id is not None and int(perm_id) > 0:
                order = await order_repo.get_by_perm_id(int(perm_id), account_id=account_id)

            if order is None:
                # Not a manual order (e.g. Engine order, handled by IBKRExecutionAdapter)
                return None, False

            # Verify account isolation
            if acct_number and acct_number != order.ibkr_account.strip().upper():
                logger.error(
                    "Account mismatch in execDetails: order account=%s, callback account=%s",
                    order.ibkr_account,
                    acct_number,
                )
                return None, False

            # Capture perm_id on manual_order if missing
            if perm_id and not order.perm_id:
                order.perm_id = int(perm_id)

            # 2. Idempotent execution insertion
            _exec_row, is_new = await exec_repo.record_execution_with_dedup(
                manual_order_id=order.id,
                exec_id=raw_exec_id,
                quantity=shares,
                price=price,
                executed_at=executed_at,
                broker_order_id=str(tws_order_id) if tws_order_id is not None else order.broker_order_id,
                commission=buffered_commission,
                commission_currency=buffered_currency,
            )

            if not is_new:
                # Duplicate callback! Do NOT mutate position a second time.
                logger.info("Duplicate execDetails received and ignored: exec_id=%s", raw_exec_id)
                await audit_repo.record_event(
                    account_id=order.account_id,
                    action="MANUAL_EXECUTION_DUPLICATE",
                    payload={"exec_id": raw_exec_id, "internal_order_id": order.internal_order_id},
                )
                return order, False

            # 3. Apply position mutation exactly once
            pos, pnl_delta = await pos_repo.apply_execution(
                account_id=order.account_id,
                trade_id=order.trade_id,
                symbol=order.symbol,
                con_id=order.con_id,
                sec_type=order.sec_type,
                currency=order.currency,
                side=side,
                quantity=shares,
                price=price,
                commission=buffered_commission or Decimal(0),
            )

            # 4. Update order fill state
            all_execs = await exec_repo.list_for_order(order.id)
            total_filled = sum(e.quantity for e in all_execs)
            now = datetime.now(UTC)

            if total_filled >= order.quantity:
                new_status = "FILLED"
                completed_at = now
            elif order.status == "CANCELLED":
                # Invariant: An order that was cancelled at the broker does not revert to PARTIALLY_FILLED
                # when a late partial fill arrives. It remains CANCELLED with executed shares recorded.
                logger.info(
                    "Late partial execution received for cancelled order %s (filled %s / %s). Preserving CANCELLED status.",
                    order.internal_order_id,
                    total_filled,
                    order.quantity,
                )
                new_status = "CANCELLED"
                completed_at = order.completed_at or now
            elif order.status in ("REJECTED", "ERROR"):
                new_status = order.status
                completed_at = order.completed_at or now
            else:
                new_status = "PARTIALLY_FILLED"
                completed_at = None

            await order_repo.update_status(
                order.id,
                status=new_status,
                completed_at=completed_at,
            )
            order.status = new_status
            order.filled_quantity = total_filled

            # 5. Record into Trade Book (trade_executions)
            line = BrokerExecutionLine(
                exec_id=raw_exec_id,
                executed_at=executed_at_iso,
                ibkr_account=order.ibkr_account,
                symbol=order.symbol,
                sec_type=order.sec_type,
                currency=order.currency,
                exchange=order.exchange,
                con_id=order.con_id,
                side=side,
                quantity=float(shares),
                price=float(price),
                cum_qty=float(total_filled),
                avg_price=float(price),
                broker_order_id=int(tws_order_id) if tws_order_id is not None else None,
                perm_id=int(perm_id) if perm_id is not None else None,
                client_id=None,
                commission=float(buffered_commission) if buffered_commission is not None else None,
                commission_currency=buffered_currency,
                realized_pnl=float(pnl_delta) if pnl_delta != Decimal(0) else None,
            )
            try:
                await trade_exec_repo.upsert_one(
                    line, account_id=order.account_id, order_id=None
                )
            except Exception:
                logger.exception("Failed to mirror manual execution to Trade Book")

            # 6. Audit event
            await audit_repo.record_event(
                account_id=order.account_id,
                action="MANUAL_EXECUTION_RECEIVED",
                payload={
                    "internal_order_id": order.internal_order_id,
                    "exec_id": raw_exec_id,
                    "quantity": str(shares),
                    "price": str(price),
                    "side": side,
                    "position_signed_qty": str(pos.signed_qty),
                    "position_avg_cost": str(pos.avg_cost),
                    "position_realized_pnl": str(pos.realized_pnl),
                    "realized_pnl_delta": str(pnl_delta),
                    "order_status": new_status,
                },
            )
            return order, True

    async def handle_commission_report(
        self,
        *,
        commission_report: Any,
    ) -> bool:
        """Handle commissionReport callback, applying commission exactly once."""
        raw_exec_id = str(getattr(commission_report, "execId", "") or "").strip()
        if not raw_exec_id:
            return False

        commission = Decimal(str(getattr(commission_report, "commission", 0.0) or 0.0))
        currency = str(getattr(commission_report, "currency", "") or "USD")

        async with self._session_factory() as session, session.begin():
            exec_repo = ManualExecutionRepository(session)
            pos_repo = ManualPositionRepository(session)
            order_repo = ManualOrderRepository(session)
            audit_repo = ManualAuditRepository(session)

            exec_row = await exec_repo.get_by_exec_id(raw_exec_id)
            if exec_row is None:
                # Commission arrived BEFORE execDetails: buffer it
                with self._lock:
                    self._pending_commissions[raw_exec_id] = {
                        "commission": commission,
                        "currency": currency,
                    }
                logger.info("Buffered out-of-order commissionReport: exec_id=%s, commission=%s", raw_exec_id, commission)
                return True

            # Commission arrived AFTER execDetails: update execution and decrement realized PnL
            _, was_updated = await exec_repo.update_commission(
                raw_exec_id,
                commission=commission,
                commission_currency=currency,
            )
            if not was_updated:
                # Commission was already applied; ignore duplicate
                return False

            order = await order_repo.get_by_id(exec_row.manual_order_id)
            if order is not None:
                pos = await pos_repo.get_by_trade_id(order.account_id, order.trade_id)
                if pos is not None:
                    pos.realized_pnl = pos.realized_pnl - commission
                    await session.flush()

                await audit_repo.record_event(
                    account_id=order.account_id,
                    action="MANUAL_COMMISSION_RECEIVED",
                    payload={
                        "internal_order_id": order.internal_order_id,
                        "exec_id": raw_exec_id,
                        "commission": str(commission),
                        "currency": currency,
                    },
                )
            return True
