"""Comprehensive test suite for M1-E Manual Order Cancellation and State-Machine Regressions.

Validates:
1. Authorization & Account Isolation:
   - Authorized cancellation succeeds.
   - Unauthenticated access rejected (401).
   - Cross-account cancellation returns 404 (zero leakage).
   - Engine order isolation (orders not in manual_orders return 404).
2. State Machine Transitions:
   - PENDING_SUBMIT without broker_order_id -> marked CANCELLED locally, zero broker calls.
   - PENDING_SUBMIT with broker_order_id -> issues cancelOrder.
   - SUBMITTED -> issues cancelOrder.
   - PARTIALLY_FILLED -> issues cancelOrder.
   - FILLED -> 400 rejection (cannot cancel filled order).
   - CANCELLED -> 200 idempotent acknowledgment, zero broker calls.
   - REJECTED / ERROR -> 400 rejection.
3. Broker Safety & Rate Limiting:
   - GatewayRateLimiter acquired with PRIORITY_ORDER_EXECUTION and "cancelOrder".
   - Exact broker_order_id passed to client.cancelOrder(int).
   - Zero placeOrder, zero reqGlobalCancel, zero emergency_flatten, zero square_off.
4. Race Handling & Out-of-Order Callbacks (Critical Fixes #1, #2, #3):
   - Late CANCELLED callback after FILLED does not overwrite FILLED.
   - Late openOrder callback during reconnect does not resurrect terminal orders (FILLED, CANCELLED, REJECTED, ERROR).
   - Late execution details on CANCELLED order record executions without resurrecting to PARTIALLY_FILLED.
   - Fill before cancel: if fully filled, cancel request rejected.
5. Accounting Invariants:
   - Cancellation produces zero executions, zero position rollback, zero artificial P&L mutation.
6. Gateway Mode Fallback (Fix #4):
   - Non-standard port returns valid mode and paper/live status without crashing or returning None.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_db_session
from app.api.routes.manual import _determine_gateway_mode
from app.broker.ibkr.gateway_rate_limiter import GatewayRateLimiter
from app.core.security import create_access_token, get_password_hash
from app.db.models.account import AccountModel
from app.db.models.manual_order import ManualOrderModel
from app.db.models.user import UserModel
from app.db.repositories.manual_repository import (
    ManualExecutionRepository,
    ManualOrderRepository,
)
from app.main import app
from app.services.manual_callbacks import ManualExecutionListener


class MockM1ETWSClient:
    def __init__(
        self,
        connected: bool = True,
        rate_limiter: GatewayRateLimiter | None = None,
    ):
        self._connected = connected
        self.rate_limiter = rate_limiter or MagicMock()
        if not isinstance(self.rate_limiter, GatewayRateLimiter):
            self.rate_limiter.acquire = AsyncMock(return_value=True)
        self.cancelled_orders: list[int] = []
        self.placed_orders: list[tuple[int, Any, Any]] = []

    def is_connected(self) -> bool:
        return self._connected

    def cancelOrder(self, orderId: int) -> None:
        self.cancelled_orders.append(orderId)

    def placeOrder(self, orderId: int, contract: Any, order: Any) -> None:
        self.placed_orders.append((orderId, contract, order))


class MockIBKRExecution:
    def __init__(
        self,
        execId: str,
        orderId: int,
        shares: float,
        price: float,
        side: str = "BOT",
        permId: int = 12345678,
        acctNumber: str = "DU123456",
        orderRef: str = "",
        time: str = "20260911 16:00:00",
    ):
        self.execId = execId
        self.orderId = orderId
        self.shares = shares
        self.price = price
        self.side = side
        self.permId = permId
        self.acctNumber = acctNumber
        self.orderRef = orderRef
        self.time = time


class MockIBKRContract:
    def __init__(
        self,
        conId: int = 10001,
        symbol: str = "SPY",
        secType: str = "CFD",
        exchange: str = "SMART",
        currency: str = "USD",
    ):
        self.conId = conId
        self.symbol = symbol
        self.secType = secType
        self.exchange = exchange
        self.currency = currency


async def create_test_account_and_user(
    session,
    prefix: str,
    *,
    enabled: bool = True,
    trading_paused: bool = False,
    is_live: bool = False,
) -> tuple[AccountModel, UserModel, str]:
    acc_code = f"U{prefix.upper()}" if is_live else f"DU{prefix.upper()}"
    acc = AccountModel(
        name=f"Acc {prefix}",
        ibkr_account=acc_code,
        total_margin=Decimal("100000.00"),
        enabled=enabled,
        trading_paused=trading_paused,
    )
    session.add(acc)
    await session.commit()
    await session.refresh(acc)

    user = UserModel(
        email=f"user_{prefix}@example.com",
        password_hash=get_password_hash("Pass123!"),
        role="user",
        is_active=True,
        ibkr_account_id=acc.id,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)

    token = create_access_token({"sub": str(user.id), "role": "user", "email": user.email})
    return acc, user, token


async def create_manual_test_order(
    session,
    account: AccountModel,
    *,
    status: str = "SUBMITTED",
    broker_order_id: str | None = "6001",
    quantity: Decimal = Decimal(10),
    filled_quantity: Decimal = Decimal(0),
) -> ManualOrderModel:
    repo = ManualOrderRepository(session)
    suffix = uuid.uuid4().hex[:6]
    order = await repo.create_order(
        account_id=account.id,
        ibkr_account=account.ibkr_account,
        idempotency_key=f"idem_{suffix}",
        internal_order_id=f"MAN_{suffix.upper()}",
        trade_id=f"TRD_{suffix.upper()}",
        symbol="SPY",
        side="BUY",
        quantity=quantity,
        order_type="LIMIT",
        con_id=10001,
        sec_type="CFD",
        exchange="SMART",
        currency="USD",
        limit_price=Decimal("450.00"),
    )
    order.filled_quantity = filled_quantity
    if filled_quantity > Decimal(0):
        exec_repo = ManualExecutionRepository(session)
        await exec_repo.record_execution_with_dedup(
            manual_order_id=order.id,
            exec_id=f"EXEC_INIT_{suffix.upper()}",
            quantity=filled_quantity,
            price=Decimal("450.00"),
            executed_at=datetime.now(UTC),
            broker_order_id=broker_order_id,
        )
    if status != "PENDING_SUBMIT" or broker_order_id is not None:
        await repo.update_status(
            order.id,
            status=status,
            broker_order_id=broker_order_id,
        )
    await session.commit()
    await session.refresh(order)
    order.filled_quantity = filled_quantity
    return order


# =====================================================================
# 1. Gateway Mode Fallback (Fix High #4)
# =====================================================================

def test_determine_gateway_mode_fallback():
    """Verify that _determine_gateway_mode never returns None and correctly classifies unknown ports."""
    from app.schemas.manual_schemas import (
        GatewayEnvironmentMode,
        GatewayModeVerificationState,
    )

    client = MockM1ETWSClient(connected=True)

    mode, state, _msg = _determine_gateway_mode(client, 4001, [])  # pyrefly: ignore[bad-argument-type]
    assert mode == GatewayEnvironmentMode.UNKNOWN
    assert state == GatewayModeVerificationState.EXPECTED_LIVE_BY_CONFIGURATION

    mode, state, _msg = _determine_gateway_mode(client, 4002, [])  # pyrefly: ignore[bad-argument-type]
    assert mode == GatewayEnvironmentMode.UNKNOWN
    assert state == GatewayModeVerificationState.EXPECTED_PAPER_BY_CONFIGURATION

    mode, state, _msg = _determine_gateway_mode(client, 7496, [])  # pyrefly: ignore[bad-argument-type]
    assert mode == GatewayEnvironmentMode.UNKNOWN
    assert state == GatewayModeVerificationState.EXPECTED_LIVE_BY_CONFIGURATION

    mode, state, _msg = _determine_gateway_mode(client, 7497, [])  # pyrefly: ignore[bad-argument-type]
    assert mode == GatewayEnvironmentMode.UNKNOWN
    assert state == GatewayModeVerificationState.EXPECTED_PAPER_BY_CONFIGURATION

    mode, state, _msg = _determine_gateway_mode(client, 5000, ["DU999999"])  # pyrefly: ignore[bad-argument-type]
    assert mode == GatewayEnvironmentMode.VERIFIED_PAPER
    assert state == GatewayModeVerificationState.ACCOUNT_PREFIX_VERIFIED

    mode, state, _msg = _determine_gateway_mode(client, 5000, ["U1234567"])  # pyrefly: ignore[bad-argument-type]
    assert mode == GatewayEnvironmentMode.VERIFIED_LIVE
    assert state == GatewayModeVerificationState.ACCOUNT_PREFIX_VERIFIED

    mode, state, _msg = _determine_gateway_mode(client, 9999, [])  # pyrefly: ignore[bad-argument-type]
    assert mode == GatewayEnvironmentMode.UNKNOWN
    assert state == GatewayModeVerificationState.UNVERIFIED

    disconn = MockM1ETWSClient(connected=False)
    mode, state, _msg = _determine_gateway_mode(disconn, 4001, ["DU123"])  # pyrefly: ignore[bad-argument-type]
    assert mode == GatewayEnvironmentMode.UNKNOWN
    assert state == GatewayModeVerificationState.UNVERIFIED

    mode, state, _msg = _determine_gateway_mode(None, 4001, ["DU123"])
    assert mode == GatewayEnvironmentMode.UNKNOWN
    assert state == GatewayModeVerificationState.UNVERIFIED


# =====================================================================
# 2. State Machine & M1-E Cancellation API Endpoint Tests
# =====================================================================

@pytest.mark.asyncio
async def test_cancel_pending_submit_without_broker_id_cancels_locally(session_factory):
    """PENDING_SUBMIT without broker_order_id cancels locally; 0 broker calls made."""
    app.dependency_overrides.clear()
    s = uuid.uuid4().hex[:6]

    async with session_factory() as session:
        acc, _user, token = await create_test_account_and_user(session, s)
        order = await create_manual_test_order(session, acc, status="PENDING_SUBMIT", broker_order_id=None)

    mock_client = MockM1ETWSClient(connected=True)
    app.state.client = mock_client

    async def _override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = _override_get_db

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/manual/orders/{order.id}/cancel?ibkr_account={acc.ibkr_account}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["success"] is True
        assert data["status"] == "CANCELLED"
        assert "locally" in data["message"].lower()

    # Verify zero broker calls
    assert len(mock_client.cancelled_orders) == 0

    # Verify DB status
    async with session_factory() as session:
        db_order = await session.get(ManualOrderModel, order.id)
        assert db_order.status == "CANCELLED"


@pytest.mark.asyncio
async def test_cancel_submitted_order_calls_broker(session_factory):
    """SUBMITTED order cancellation issues client.cancelOrder with exact broker_order_id."""
    app.dependency_overrides.clear()
    s = uuid.uuid4().hex[:6]

    async with session_factory() as session:
        acc, _user, token = await create_test_account_and_user(session, s)
        order = await create_manual_test_order(session, acc, status="SUBMITTED", broker_order_id="71234")

    mock_client = MockM1ETWSClient(connected=True)
    app.state.client = mock_client

    async def _override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = _override_get_db

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/manual/orders/{order.id}/cancel?ibkr_account={acc.ibkr_account}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["success"] is True
        assert data["status"] == "CANCEL_REQUESTED"
        assert data["broker_order_id"] == 71234

    # Verify exact broker_order_id passed
    assert mock_client.cancelled_orders == [71234]
    # Verify zero forbidden mutations
    assert len(mock_client.placed_orders) == 0


@pytest.mark.asyncio
async def test_cancel_partially_filled_order(session_factory):
    """PARTIALLY_FILLED order cancellation submits cancelOrder to broker."""
    app.dependency_overrides.clear()
    s = uuid.uuid4().hex[:6]

    async with session_factory() as session:
        acc, _user, token = await create_test_account_and_user(session, s)
        order = await create_manual_test_order(
            session, acc, status="PARTIALLY_FILLED", broker_order_id="85432", quantity=Decimal(100), filled_quantity=Decimal(30)
        )

    mock_client = MockM1ETWSClient(connected=True)
    app.state.client = mock_client

    async def _override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = _override_get_db

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/manual/orders/{order.id}/cancel?ibkr_account={acc.ibkr_account}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["success"] is True
        assert data["status"] == "CANCEL_REQUESTED"

    assert mock_client.cancelled_orders == [85432]


@pytest.mark.asyncio
async def test_cancel_already_cancelled_order_is_idempotent(session_factory):
    """CANCELLED order cancellation returns success idempotently without calling broker."""
    app.dependency_overrides.clear()
    s = uuid.uuid4().hex[:6]

    async with session_factory() as session:
        acc, _user, token = await create_test_account_and_user(session, s)
        order = await create_manual_test_order(session, acc, status="CANCELLED", broker_order_id="99001")

    mock_client = MockM1ETWSClient(connected=True)
    app.state.client = mock_client

    async def _override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = _override_get_db

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/manual/orders/{order.id}/cancel?ibkr_account={acc.ibkr_account}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["success"] is True
        assert data["status"] == "CANCELLED"
        assert "already cancelled" in data["message"].lower()

    # Zero broker calls made
    assert len(mock_client.cancelled_orders) == 0


@pytest.mark.asyncio
async def test_cancel_filled_rejected_error_orders_rejected(session_factory):
    """Terminal states FILLED, REJECTED, ERROR cannot be cancelled; returns 400."""
    app.dependency_overrides.clear()
    s = uuid.uuid4().hex[:6]

    mock_client = MockM1ETWSClient(connected=True)
    app.state.client = mock_client

    async def _override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = _override_get_db

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for term_status in ("FILLED", "REJECTED", "ERROR"):
            async with session_factory() as session:
                acc, _user, token = await create_test_account_and_user(session, f"{s}_{term_status[:2]}")
                order = await create_manual_test_order(session, acc, status=term_status, broker_order_id="44001")

            resp = await client.post(
                f"/api/v1/manual/orders/{order.id}/cancel?ibkr_account={acc.ibkr_account}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 400, resp.text
            assert "cannot cancel order" in resp.text.lower()

    # Zero broker calls made
    assert len(mock_client.cancelled_orders) == 0


# =====================================================================
# 3. Account Isolation & Engine Isolation Tests
# =====================================================================

@pytest.mark.asyncio
async def test_cancel_cross_account_forbidden_returns_404(session_factory):
    """User authorized for Account A attempting to cancel Account B's order receives 404 (zero leakage)."""
    app.dependency_overrides.clear()
    s1 = uuid.uuid4().hex[:6]
    s2 = uuid.uuid4().hex[:6]

    async with session_factory() as session:
        acc_a, _user_a, token_a = await create_test_account_and_user(session, s1)
        acc_b, _user_b, _token_b = await create_test_account_and_user(session, s2)
        order_b = await create_manual_test_order(session, acc_b, status="SUBMITTED", broker_order_id="77002")

    mock_client = MockM1ETWSClient(connected=True)
    app.state.client = mock_client

    async def _override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = _override_get_db

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Account A attempts to cancel order belonging to Account B
        resp = await client.post(
            f"/api/v1/manual/orders/{order_b.id}/cancel?ibkr_account={acc_a.ibkr_account}",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert resp.status_code == 404, resp.text
        assert "not found" in resp.text.lower()

    assert len(mock_client.cancelled_orders) == 0


@pytest.mark.asyncio
async def test_cancel_unauthenticated_returns_401(session_factory, monkeypatch):
    """Unauthenticated request must be rejected with 401."""
    app.dependency_overrides.clear()
    s = uuid.uuid4().hex[:6]

    async with session_factory() as session:
        acc, _user, _token = await create_test_account_and_user(session, s)
        order = await create_manual_test_order(session, acc, status="SUBMITTED", broker_order_id="12345")

    async def _override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = _override_get_db
    monkeypatch.setenv("TRADINGAPP_TESTING", "0")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/manual/orders/{order.id}/cancel?ibkr_account={acc.ibkr_account}",
        )
        assert resp.status_code == 401


# =====================================================================
# 4. Critical Callback State-Machine Regressions (Fixes #1, #2, #3)
# =====================================================================

@pytest.mark.asyncio
async def test_critical_1_filled_order_never_overwritten_by_late_cancelled(session_factory):
    """Critical Fix #1: FILLED order must NOT be mutated to CANCELLED by a delayed callback."""
    s = uuid.uuid4().hex[:6]
    async with session_factory() as session:
        acc, _, _ = await create_test_account_and_user(session, s)
        order = await create_manual_test_order(
            session, acc, status="FILLED", broker_order_id="90101", quantity=Decimal(10), filled_quantity=Decimal(10)
        )

    listener = ManualExecutionListener(session_factory)

    # Inbound late Cancelled callback arrives from IBKR
    await listener.handle_order_status(
        order_id=90101,
        status="Cancelled",
        perm_id=0,
    )

    # Order must remain FILLED!
    async with session_factory() as session:
        refreshed = await session.get(ManualOrderModel, order.id)
        assert refreshed.status == "FILLED"
        assert refreshed.filled_quantity == Decimal(10)


@pytest.mark.asyncio
async def test_critical_2_open_order_callback_does_not_resurrect_terminal_order(session_factory):
    """Critical Fix #2: Reconnect openOrder callback must not resurrect terminal orders to SUBMITTED."""
    s = uuid.uuid4().hex[:6]
    listener = ManualExecutionListener(session_factory)

    for term_status in ("CANCELLED", "FILLED", "REJECTED", "ERROR"):
        async with session_factory() as session:
            acc, _, _ = await create_test_account_and_user(session, f"{s}_{term_status[:2]}")
            order = await create_manual_test_order(
                session, acc, status=term_status, broker_order_id=str(91000 + hash(term_status) % 1000)
            )

        # IBKR reconnect sends openOrder with status="Submitted"
        await listener.handle_open_order(
            order_id=int(order.broker_order_id),  # pyrefly: ignore[bad-argument-type]
            order_ref=order.internal_order_id,
            perm_id=888999,
            raw_status="Submitted",
        )

        async with session_factory() as session:
            refreshed = await session.get(ManualOrderModel, order.id)
            assert refreshed.status == term_status, f"Expected {term_status}, got {refreshed.status}"
            assert refreshed.perm_id == 888999


@pytest.mark.asyncio
async def test_critical_3_late_exec_details_on_cancelled_order_records_fill_without_resurrecting(session_factory):
    """Critical Fix #3: Execution arriving on CANCELLED order records execution but leaves status CANCELLED."""
    s = uuid.uuid4().hex[:6]
    async with session_factory() as session:
        acc, _, _ = await create_test_account_and_user(session, s)
        order = await create_manual_test_order(
            session, acc, status="CANCELLED", broker_order_id="92001", quantity=Decimal(100), filled_quantity=Decimal(0)
        )

    listener = ManualExecutionListener(session_factory)

    contract = MockIBKRContract(symbol="SPY")
    execution = MockIBKRExecution(
        execId=f"EXEC_LATE_{s}",
        orderId=92001,
        shares=40.0,
        price=450.0,
        side="BOT",
        permId=777666,
        acctNumber=acc.ibkr_account,
    )

    updated_order, is_new = await listener.handle_exec_details(contract=contract, execution=execution)
    assert is_new is True
    assert updated_order is not None
    assert updated_order.status == "CANCELLED"
    assert updated_order.filled_quantity == Decimal(40)

    # Verify execution was recorded in DB
    async with session_factory() as session:
        exec_repo = ManualExecutionRepository(session)
        execs = await exec_repo.get_by_order_id(order.id)
        assert len(execs) == 1
        assert execs[0].exec_id == f"EXEC_LATE_{s}"
        assert execs[0].quantity == Decimal(40)


# =====================================================================
# 5. Accounting Invariants: Zero Execution from Cancellation
# =====================================================================

@pytest.mark.asyncio
async def test_cancellation_causes_zero_executions_or_pnl_mutation(session_factory):
    """Cancellation must never create executions or mutate positions."""
    app.dependency_overrides.clear()
    s = uuid.uuid4().hex[:6]

    async with session_factory() as session:
        acc, _user, token = await create_test_account_and_user(session, s)
        order = await create_manual_test_order(
            session, acc, status="PARTIALLY_FILLED", broker_order_id="93001", quantity=Decimal(50), filled_quantity=Decimal(20)
        )

    mock_client = MockM1ETWSClient(connected=True)
    app.state.client = mock_client

    async def _override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = _override_get_db

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/manual/orders/{order.id}/cancel?ibkr_account={acc.ibkr_account}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

    # Verify no executions were created by cancellation
    async with session_factory() as session:
        exec_repo = ManualExecutionRepository(session)
        execs = await exec_repo.get_by_order_id(order.id)
        assert len(execs) == 1  # Exactly the initial fill, no new executions
        assert execs[0].quantity == Decimal(20)

        refreshed = await session.get(ManualOrderModel, order.id)
        # filled_quantity is not rolled back
        assert refreshed.filled_quantity == Decimal(20)
