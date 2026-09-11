"""Manual Trading Service (M1-B).

Orchestrates:
1. Server-side pre-trade validation (account enabled, manual halt, kill switch, trading pause, contract validation, minTick, outsideRth=False).
2. Non-mutating order preview with notional and IBKR what-if margin probe.
3. Durable, idempotent order submission with two-phase commit:
   PENDING_SUBMIT (committed to PostgreSQL) -> GatewayRateLimiter -> allocate_next_order_id -> TWSClient.placeOrder() -> SUBMITTED.
4. Environment-agnostic execution: executes against whichever IBKR connection is configured (Paper or Live).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import HTTPException
from ibapi.contract import Contract  # type: ignore[import-untyped]
from ibapi.order import Order as IBOrder  # type: ignore[import-untyped]
from sqlalchemy.ext.asyncio import AsyncSession

from app.broker.ibkr.gateway_rate_limiter import (
    PRIORITY_ORDER_EXECUTION,
    GatewayRateLimiter,
)
from app.broker.ibkr.tws_client import TWSClient
from app.db.models.account import AccountModel
from app.db.models.user import UserModel
from app.db.repositories.manual_repository import (
    ManualAuditRepository,
    ManualHaltRepository,
    ManualOrderRepository,
    ManualPositionRepository,
)
from app.oms.ibkr_adapter import IBKRExecutionAdapter, WhatIfResult
from app.schemas.manual_schemas import (
    GatewayEnvironmentMode,
    ManualOrderCancelResponse,
    ManualOrderPreviewRequest,
    ManualOrderPreviewResponse,
    ManualOrderRead,
    ManualOrderSubmitRequest,
    ManualOrderSubmitResponse,
)
from app.services.kill_switch import (
    get_armed_kill_switch_operation,
    is_account_kill_switch_active,
)
from app.services.trading_pause import is_account_trading_paused

logger = logging.getLogger(__name__)


class ManualTradingService:
    """Service handling manual order preview and execution."""

    def __init__(
        self,
        session: AsyncSession,
        client: TWSClient | None = None,
        ibkr_adapter: IBKRExecutionAdapter | None = None,
    ) -> None:
        self._session = session
        self._client = client
        self._ibkr_adapter = ibkr_adapter
        self._order_repo = ManualOrderRepository(session)
        self._pos_repo = ManualPositionRepository(session)
        self._halt_repo = ManualHaltRepository(session)
        self._audit_repo = ManualAuditRepository(session)

    async def validate_pretrade(
        self,
        account: AccountModel,
        request: ManualOrderPreviewRequest | ManualOrderSubmitRequest,
    ) -> tuple[bool, list[str], list[str]]:
        """Validate order parameters against all hard safety gates.

        Returns (valid, errors, warnings).
        """
        errors: list[str] = []
        warnings: list[str] = []

        # 1. Gateway connectivity check
        if self._client is None or not self._client.is_connected():
            errors.append("IBKR Gateway is disconnected. Orders cannot be processed.")

        # 2. Account enabled check
        if not account.enabled:
            errors.append(f"Account {account.ibkr_account} is disabled. Trading is blocked.")

        # 3. Manual halt check
        halt_state = await self._halt_repo.get_halt_state(account.id)
        if halt_state and halt_state.halted:
            errors.append(
                f"Account {account.ibkr_account} is manually halted: {halt_state.reason or 'No reason provided'}."
            )

        # 4. Kill switch check
        if is_account_kill_switch_active(account.id):
            errors.append(f"Account {account.ibkr_account} kill switch is active. Trading is blocked.")
        else:
            armed_op = await get_armed_kill_switch_operation(self._session, account.id)
            if armed_op is not None:
                errors.append(
                    f"Account {account.ibkr_account} has an armed emergency kill-switch operation ({armed_op.status})."
                )

        # 5. Trading pause check
        # Pause blocks new OPEN orders. An order is only permitted while paused
        # if it genuinely reduces an existing open manual position.
        is_paused = is_account_trading_paused(account.id) or account.trading_paused
        if is_paused:
            open_positions = await self._pos_repo.list_open_for_account(account.id)
            # Match position by trade_id if supplied, or by symbol & con_id
            matched_pos = None
            for p in open_positions:
                if request.trade_id and p.trade_id == request.trade_id:
                    matched_pos = p
                    break
                if p.symbol == request.symbol.strip().upper() and p.con_id == request.con_id:
                    matched_pos = p
                    break

            is_reducing = False
            if matched_pos is not None and (
                (
                    matched_pos.signed_qty > 0
                    and request.side == "SELL"
                    and request.quantity <= matched_pos.signed_qty
                )
                or (
                    matched_pos.signed_qty < 0
                    and request.side == "BUY"
                    and request.quantity <= abs(matched_pos.signed_qty)
                )
            ):
                is_reducing = True

            if not is_reducing:
                errors.append(
                    f"Account {account.ibkr_account} is trading paused. New OPEN orders are blocked."
                )

        # 6. Contract identity validation
        if request.sec_type != "CFD":
            errors.append(f"Invalid security type '{request.sec_type}'. M1 supports CFD contracts only.")
        if request.con_id <= 0:
            errors.append(f"Invalid contract con_id '{request.con_id}'. Must be a positive integer.")
        if not request.symbol.strip():
            errors.append("Contract symbol is required.")
        if not request.exchange.strip():
            errors.append("Exchange is required.")
        if not request.currency.strip():
            errors.append("Currency is required.")

        # 7. Order type validation
        order_type = request.order_type.strip().upper()
        if order_type == "STOP":
            errors.append("STOP order type is not executable in M1-B. Only MARKET and LIMIT orders are supported.")
        elif order_type not in ("LIMIT", "MARKET"):
            errors.append(f"Unsupported order type '{order_type}'. Supported types: LIMIT, MARKET.")

        # 8. Quantity validation
        if request.quantity <= 0:
            errors.append("Quantity must be greater than zero.")

        # 9. Price and minTick validation
        if order_type == "LIMIT":
            if request.limit_price is None or request.limit_price <= 0:
                errors.append("Limit price is required and must be greater than zero for LIMIT orders.")
            else:
                if request.min_tick is not None and request.min_tick > 0:
                    min_tick_dec = Decimal(str(request.min_tick))
                    ticks = request.limit_price / min_tick_dec
                    # Check if ticks is essentially an integer
                    if abs(ticks - round(ticks)) > Decimal("1e-5"):
                        errors.append(
                            f"Limit price {request.limit_price} does not conform to minimum tick {request.min_tick}."
                        )

        return len(errors) == 0, errors, warnings

    async def preview_order(
        self,
        account: AccountModel,
        request: ManualOrderPreviewRequest,
        env_mode: GatewayEnvironmentMode,
    ) -> ManualOrderPreviewResponse:
        """Non-mutating order preview. Calculates notional and runs what-if margin probe."""
        valid, errors, warnings = await self.validate_pretrade(account, request)

        connected = self._client is not None and self._client.is_connected()
        halt_state = await self._halt_repo.get_halt_state(account.id)
        manual_halted = bool(halt_state and halt_state.halted)
        kill_switch_active = is_account_kill_switch_active(account.id)
        trading_paused = is_account_trading_paused(account.id) or account.trading_paused

        effective_price: Decimal | None = None
        notional: Decimal | None = None
        notional_status = "NO_MARKET_PRICE"

        if request.order_type == "LIMIT" and request.limit_price and request.limit_price > 0:
            effective_price = request.limit_price
            notional = request.quantity * request.limit_price
            notional_status = "EXACT"

        init_margin_change: Decimal | None = None
        maint_margin_change: Decimal | None = None
        margin_status = "SKIPPED"

        # If order parameters are valid and connected, probe what-if margin
        if valid and connected and self._ibkr_adapter is not None:
            # Need a price for what-if probe. If market order and price is None, probe with a dummy price
            # or skip what-if
            probe_price = effective_price if effective_price is not None else Decimal("100.00")
            contract = Contract()
            contract.conId = request.con_id
            contract.symbol = request.symbol.strip().upper()
            contract.secType = "CFD"
            contract.exchange = request.exchange.strip().upper() or "SMART"
            contract.currency = request.currency.strip().upper() or "USD"

            try:
                whatif_result: WhatIfResult = await self._ibkr_adapter.probe_margin(
                    contract=contract,
                    side=request.side,
                    quantity=request.quantity,
                    price=probe_price,
                    ibkr_account=account.ibkr_account,
                    timeout=5.0,
                )
                if not whatif_result.unknown:
                    init_margin_change = whatif_result.init_margin_change
                    maint_margin_change = whatif_result.maint_margin_change
                    margin_status = "AVAILABLE"
                else:
                    margin_status = "UNAVAILABLE"
                    warnings.append("What-if margin pre-check returned unavailable or timed out.")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error during margin probe for %s: %s", request.symbol, exc)
                margin_status = "UNAVAILABLE"
                warnings.append("What-if margin pre-check could not be completed.")

        return ManualOrderPreviewResponse(
            valid=valid,
            symbol=request.symbol.strip().upper(),
            con_id=request.con_id,
            sec_type=request.sec_type,
            exchange=request.exchange.strip().upper(),
            currency=request.currency.strip().upper(),
            side=request.side,
            quantity=request.quantity,
            order_type=request.order_type,
            limit_price=request.limit_price,
            effective_price=effective_price,
            notional=notional,
            notional_status=notional_status,
            init_margin_change=init_margin_change,
            maint_margin_change=maint_margin_change,
            margin_status=margin_status,
            gateway_connected=connected,
            environment=env_mode,
            account_enabled=account.enabled,
            kill_switch_active=kill_switch_active,
            trading_paused=trading_paused,
            manual_halted=manual_halted,
            warnings=warnings,
            errors=errors,
        )

    async def submit_order(
        self,
        account: AccountModel,
        request: ManualOrderSubmitRequest,
        current_user: UserModel,
    ) -> ManualOrderSubmitResponse:
        """Durable, idempotent manual order submission with two-phase state machine."""
        # 1. Server-side revalidation of all safety gates
        valid, errors, _ = await self.validate_pretrade(account, request)
        if not valid:
            raise HTTPException(
                status_code=400,
                detail=f"Order validation failed: {'; '.join(errors)}",
            )

        idempotency_key = request.idempotency_key.strip()

        # 2. Idempotency barrier against PostgreSQL
        existing = await self._order_repo.get_by_idempotency_key(
            account_id=account.id, idempotency_key=idempotency_key
        )
        if existing is not None:
            # Check for identical parameters (idempotent replay) vs conflict
            is_same_param = (
                existing.con_id == request.con_id
                and existing.symbol == request.symbol.strip().upper()
                and existing.side == request.side
                and existing.quantity == request.quantity
                and existing.order_type == request.order_type
                and existing.limit_price == request.limit_price
            )
            if is_same_param:
                logger.info(
                    "Idempotent replay for manual order: account=%s key=%s order_id=%s",
                    account.ibkr_account,
                    idempotency_key,
                    existing.internal_order_id,
                )
                return ManualOrderSubmitResponse(
                    order=ManualOrderRead.model_validate(existing),
                    idempotent_replay=True,
                    message="Order previously submitted (idempotent replay). No duplicate broker order placed.",
                )
            else:
                logger.warning(
                    "Idempotency conflict for manual order: account=%s key=%s",
                    account.ibkr_account,
                    idempotency_key,
                )
                raise HTTPException(
                    status_code=409,
                    detail=f"Idempotency conflict: key '{idempotency_key}' already used with different parameters.",
                )

        # 3. Create durable PENDING_SUBMIT record in PostgreSQL and COMMIT before broker call
        internal_order_id = f"MAN_{uuid.uuid4().hex[:12].upper()}"
        trade_id = request.trade_id.strip() if request.trade_id else f"TRD_{uuid.uuid4().hex[:12].upper()}"

        order_row = await self._order_repo.create_order(
            account_id=account.id,
            ibkr_account=account.ibkr_account,
            idempotency_key=idempotency_key,
            internal_order_id=internal_order_id,
            trade_id=trade_id,
            symbol=request.symbol.strip().upper(),
            side=request.side,
            quantity=request.quantity,
            order_type=request.order_type,
            con_id=request.con_id,
            sec_type="CFD",
            exchange=request.exchange.strip().upper() or "SMART",
            currency=request.currency.strip().upper() or "USD",
            limit_price=request.limit_price,
            tif=request.tif.strip().upper() or "DAY",
            outside_rth=False,  # Enforce outsideRth=False strictly
            user_id=current_user.id,
            status="PENDING_SUBMIT",
        )
        await self._session.commit()
        await self._session.refresh(order_row)

        logger.info(
            "Durable manual order PENDING_SUBMIT created: id=%s internal_id=%s account=%s",
            order_row.id,
            internal_order_id,
            account.ibkr_account,
        )

        # 4. Acquire token from GatewayRateLimiter
        if self._client is None or not self._client.is_connected():
            await self._order_repo.update_status(
                order_row.id,
                status="ERROR",
                reject_reason="Gateway disconnected before order placement",
            )
            await self._session.commit()
            raise HTTPException(status_code=503, detail="IBKR Gateway is disconnected.")

        rate_limiter: GatewayRateLimiter | None = getattr(self._client, "_rate_limiter", None)
        if rate_limiter is not None:
            try:
                await rate_limiter.acquire(PRIORITY_ORDER_EXECUTION, "placeOrder")
            except Exception as exc:
                logger.error("Rate limiter acquisition timed out for manual order %s: %s", internal_order_id, exc)
                await self._order_repo.update_status(
                    order_row.id,
                    status="ERROR",
                    reject_reason=f"Gateway rate limiter timeout: {exc}",
                )
                await self._session.commit()
                raise HTTPException(
                    status_code=504,
                    detail="Gateway pacing timeout: order placement delayed due to rate limiting.",
                ) from exc

        # 5. Allocate broker order ID through TWSClient
        try:
            broker_order_id = self._client.allocate_next_order_id()
            order_row.broker_order_id = str(broker_order_id)
            await self._session.commit()
        except Exception as exc:
            logger.error("Failed to allocate broker order ID for %s: %s", internal_order_id, exc)
            await self._order_repo.update_status(
                order_row.id,
                status="ERROR",
                reject_reason=f"Order ID allocation failed: {exc}",
            )
            await self._session.commit()
            raise HTTPException(
                status_code=500, detail=f"Failed to allocate IBKR order ID: {exc}"
            ) from exc

        # 6. Build Contract & Order objects
        contract = Contract()
        contract.conId = request.con_id
        contract.symbol = request.symbol.strip().upper()
        contract.secType = "CFD"
        contract.exchange = request.exchange.strip().upper() or "SMART"
        contract.currency = request.currency.strip().upper() or "USD"

        ib_order = IBOrder()
        ib_order.action = request.side.strip().upper()
        ib_order.totalQuantity = float(request.quantity)  # pyrefly: ignore[bad-assignment]
        ib_order.orderType = "LMT" if request.order_type == "LIMIT" else "MKT"
        if request.order_type == "LIMIT" and request.limit_price is not None:
            ib_order.lmtPrice = float(request.limit_price)
        ib_order.tif = request.tif.strip().upper() or "DAY"
        ib_order.outsideRth = False  # Strict enforcement
        ib_order.transmit = True
        ib_order.account = account.ibkr_account
        ib_order.orderRef = internal_order_id

        # 7. Call TWSClient.placeOrder()
        try:
            self._client.placeOrder(broker_order_id, contract, ib_order)
            logger.info(
                "TWSClient.placeOrder invoked: internal_id=%s broker_order_id=%s symbol=%s",
                internal_order_id,
                broker_order_id,
                contract.symbol,
            )
        except Exception as exc:
            logger.exception("TWSClient.placeOrder raised exception for %s", internal_order_id)
            await self._order_repo.update_status(
                order_row.id,
                status="ERROR",
                reject_reason=f"placeOrder exception: {exc}",
            )
            await self._session.commit()
            raise HTTPException(
                status_code=502, detail=f"Failed to submit order to IBKR Gateway: {exc}"
            ) from exc

        # 8. Update status to SUBMITTED and record audit event
        # CRITICAL: SUBMITTED does NOT mean FILLED. Fills belong to broker callbacks in M1-C.
        now = datetime.now(UTC)
        updated_row = await self._order_repo.update_status(
            order_row.id,
            status="SUBMITTED",
            broker_order_id=str(broker_order_id),
            submitted_at=now,
        )
        await self._audit_repo.record_event(
            account_id=account.id,
            user_id=current_user.id,
            action="MANUAL_ORDER_SUBMITTED",
            request_id=internal_order_id,
            payload={
                "internal_order_id": internal_order_id,
                "broker_order_id": str(broker_order_id),
                "symbol": contract.symbol,
                "con_id": contract.conId,
                "side": ib_order.action,
                "quantity": str(request.quantity),
                "order_type": ib_order.orderType,
                "limit_price": str(request.limit_price) if request.limit_price else None,
                "idempotency_key": idempotency_key,
                "source": "manual",
            },
        )
        await self._session.commit()
        if updated_row:
            await self._session.refresh(updated_row)

        return ManualOrderSubmitResponse(
            order=ManualOrderRead.model_validate(updated_row or order_row),
            idempotent_replay=False,
            message="Order placed with IBKR Gateway and recorded as SUBMITTED.",
        )

    async def cancel_order(
        self,
        account: AccountModel,
        order_id: int,
        current_user: UserModel,
    ) -> ManualOrderCancelResponse:
        """Cancel an eligible manual order.

        Guarantees:
        1. Account isolation: row locked with account_id filter. Non-matching -> 404.
        2. Idempotent: already CANCELLED returns HTTP 200 without duplicate broker calls.
        3. Terminal locking: FILLED, REJECTED, ERROR cannot be cancelled -> HTTP 400.
        4. Local cancel for PENDING_SUBMIT with no broker_order_id: zero broker calls.
        5. Broker cancellation: paced via GatewayRateLimiter(PRIORITY_ORDER_EXECUTION, "cancelOrder").
        6. Single broker mutation: calls client.cancelOrder(int(broker_order_id)).
        7. Zero forbidden calls (no placeOrder, reqGlobalCancel, emergency_flatten, square_off).
        """
        # 1. Fetch order with row-level lock strictly scoped to authorized account
        order = await self._order_repo.get_by_id(
            order_id, account_id=account.id, for_update=True
        )
        if order is None:
            # Return 404 to avoid leaking existence of orders across accounts
            raise HTTPException(
                status_code=404,
                detail=f"Manual order with ID {order_id} not found for account {account.ibkr_account}.",
            )

        # 2. Check current status
        # Case A: Already cancelled -> idempotent success
        if order.status == "CANCELLED":
            logger.info(
                "Cancel requested for already CANCELLED manual order %s (idempotent)",
                order.internal_order_id,
            )
            return ManualOrderCancelResponse(
                order=ManualOrderRead.model_validate(order),
                success=True,
                status="CANCELLED",
                message="Order is already cancelled.",
                broker_order_id=int(order.broker_order_id) if order.broker_order_id else None,
                perm_id=order.perm_id,
            )

        # Case B: Terminal states that cannot be cancelled
        if order.status == "FILLED":
            await self._audit_repo.record_event(
                account_id=account.id,
                user_id=current_user.id,
                action="MANUAL_ORDER_CANCEL_REJECTED",
                request_id=order.internal_order_id,
                payload={
                    "order_id": order.id,
                    "internal_order_id": order.internal_order_id,
                    "status": order.status,
                    "reason": "Order is already filled",
                },
            )
            await self._session.commit()
            raise HTTPException(
                status_code=400,
                detail=f"Cannot cancel order {order.internal_order_id}: order is already FILLED.",
            )

        if order.status in ("REJECTED", "ERROR"):
            await self._audit_repo.record_event(
                account_id=account.id,
                user_id=current_user.id,
                action="MANUAL_ORDER_CANCEL_REJECTED",
                request_id=order.internal_order_id,
                payload={
                    "order_id": order.id,
                    "internal_order_id": order.internal_order_id,
                    "status": order.status,
                    "reason": f"Order is in terminal status {order.status}",
                },
            )
            await self._session.commit()
            raise HTTPException(
                status_code=400,
                detail=f"Cannot cancel order {order.internal_order_id}: order is in terminal status '{order.status}'.",
            )

        # Case C: PENDING_SUBMIT with no broker_order_id -> Cancel locally without broker call
        if order.status == "PENDING_SUBMIT" and not order.broker_order_id:
            now = datetime.now(UTC)
            updated_order = await self._order_repo.update_status(
                order.id,
                status="CANCELLED",
                completed_at=now,
                reject_reason="Cancelled by operator prior to broker submission",
            )
            await self._audit_repo.record_event(
                account_id=account.id,
                user_id=current_user.id,
                action="MANUAL_ORDER_CANCELLED",
                request_id=order.internal_order_id,
                payload={
                    "order_id": order.id,
                    "internal_order_id": order.internal_order_id,
                    "broker_order_id": None,
                    "status_prior": "PENDING_SUBMIT",
                    "note": "Cancelled locally before broker transmission",
                },
            )
            await self._session.commit()
            if updated_order:
                await self._session.refresh(updated_order)
            return ManualOrderCancelResponse(
                order=ManualOrderRead.model_validate(updated_order or order),
                success=True,
                status="CANCELLED",
                message="Order cancelled locally before broker submission.",
                broker_order_id=None,
                perm_id=order.perm_id,
            )

        # Case D: SUBMITTED, PARTIALLY_FILLED, or PENDING_SUBMIT with broker_order_id -> Broker cancellation
        if not order.broker_order_id:
            raise HTTPException(
                status_code=500,
                detail=f"Order {order.internal_order_id} is in status '{order.status}' but lacks a broker order ID.",
            )

        try:
            tws_order_id = int(order.broker_order_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Invalid broker order ID '{order.broker_order_id}' on order {order.internal_order_id}.",
            ) from exc

        # Verify gateway connectivity
        if self._client is None or not self._client.is_connected():
            raise HTTPException(
                status_code=503,
                detail="IBKR Gateway is disconnected. Cannot cancel working order at broker.",
            )

        # Acquire GatewayRateLimiter token for cancelOrder
        rate_limiter: GatewayRateLimiter | None = getattr(
            self._client, "_rate_limiter", None
        ) or getattr(self._client, "rate_limiter", None)
        if rate_limiter is not None:
            try:
                await rate_limiter.acquire(PRIORITY_ORDER_EXECUTION, "cancelOrder")
            except Exception as exc:
                logger.error("Rate limiter acquisition timed out for cancelOrder %s: %s", order.internal_order_id, exc)
                raise HTTPException(
                    status_code=504,
                    detail="Gateway pacing timeout: cancel request delayed due to rate limiting.",
                ) from exc

        # Dispatch cancelOrder to IBKR Gateway
        try:
            self._client.cancelOrder(tws_order_id)
            logger.info(
                "TWSClient.cancelOrder dispatched: internal_id=%s broker_order_id=%d",
                order.internal_order_id,
                tws_order_id,
            )
        except Exception as exc:
            logger.exception("TWSClient.cancelOrder raised exception for %s", order.internal_order_id)
            raise HTTPException(
                status_code=502,
                detail=f"Failed to dispatch cancellation to IBKR Gateway: {exc}",
            ) from exc

        # Record audit event
        await self._audit_repo.record_event(
            account_id=account.id,
            user_id=current_user.id,
            action="MANUAL_ORDER_CANCEL_REQUESTED",
            request_id=order.internal_order_id,
            payload={
                "order_id": order.id,
                "internal_order_id": order.internal_order_id,
                "broker_order_id": str(tws_order_id),
                "perm_id": order.perm_id,
                "status_prior": order.status,
                "symbol": order.symbol,
                "quantity": str(order.quantity),
            },
        )
        await self._session.commit()
        await self._session.refresh(order)

        return ManualOrderCancelResponse(
            order=ManualOrderRead.model_validate(order),
            success=True,
            status="CANCEL_REQUESTED",
            message="Cancellation request sent to IBKR Gateway. Order status will update upon broker confirmation.",
            broker_order_id=tws_order_id,
            perm_id=order.perm_id,
        )

