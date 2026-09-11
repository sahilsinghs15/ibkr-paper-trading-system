"""Startup recovery and broker correlation reconciliation for Manual Trading (M1-C).

Scans for unresolved manual orders across backend restarts, correlates them against
broker open orders and executions, and marks ambiguous submissions safely without
automatic resubmission.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.repositories.manual_repository import (
    ManualAuditRepository,
    ManualOrderRepository,
)

logger = logging.getLogger(__name__)


class ManualTradingRecoveryService:
    """Discovers non-terminal manual orders across restarts and reconciles with broker."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: Any = None,
    ) -> None:
        self._session_factory = session_factory
        self._client = client

    async def run_startup_recovery(self) -> int:
        """Scan PostgreSQL for non-terminal manual orders on backend startup.

        Returns number of recovered/audited manual orders.
        """
        async with self._session_factory() as session, session.begin():
            order_repo = ManualOrderRepository(session)
            audit_repo = ManualAuditRepository(session)

            unresolved = await order_repo.get_unresolved_orders()
            if not unresolved:
                logger.info("Manual trading startup recovery scan complete: no unresolved orders found")
                return 0

            logger.info("Manual trading startup recovery found %d unresolved manual orders", len(unresolved))

            recovered_count = 0
            for order in unresolved:
                if order.status == "PENDING_SUBMIT":
                    # Ambiguity window: PENDING_SUBMIT was committed, but process restarted.
                    # Flag order as requiring operator review/reconciliation rather than resubmitting.
                    logger.warning(
                        "Manual order %s in PENDING_SUBMIT across restart. Setting RECOVERY_REQUIRED without resubmission.",
                        order.internal_order_id,
                    )
                    await order_repo.update_status(
                        order.id,
                        status="ERROR",
                        reject_reason="UNCONFIRMED_BROKER_SUBMISSION_REQUIRES_RECOVERY: order was pending submission across restart",
                    )
                    await audit_repo.record_event(
                        account_id=order.account_id,
                        action="MANUAL_ORDER_RECOVERY_REQUIRED",
                        payload={
                            "internal_order_id": order.internal_order_id,
                            "status_prior": "PENDING_SUBMIT",
                            "broker_order_id": order.broker_order_id,
                            "reason": "Backend restarted before broker acceptance was confirmed",
                        },
                    )
                    recovered_count += 1
                else:
                    # Order is SUBMITTED or PARTIALLY_FILLED: awaiting live broker callbacks
                    logger.info(
                        "Manual order %s is %s; awaiting broker callbacks on reconnect",
                        order.internal_order_id,
                        order.status,
                    )
                    recovered_count += 1

            return recovered_count

    def request_broker_open_orders(self) -> bool:
        """Request open orders from IBKR via reqOpenOrders (scoped to this clientId).

        Returns True if request was sent, False if client is disconnected.
        """
        client = self._client
        if client is None or not getattr(client, "is_connected", lambda: False)():
            logger.warning("request_broker_open_orders: TWSClient not connected")
            return False

        try:
            req_open = getattr(client, "reqOpenOrders", None)
            if callable(req_open):
                req_open()
                logger.info("Requested broker open orders for manual recovery")
                return True
        except Exception:
            logger.exception("Failed to request open orders from TWS")

        return False
