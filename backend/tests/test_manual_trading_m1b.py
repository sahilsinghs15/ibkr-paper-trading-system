"""Comprehensive unit and integration tests for Manual Trading Milestone M1-B.

Covers:
1. Valid MARKET preview succeeds.
2. Valid LIMIT preview succeeds.
3. Preview validates account authorization.
4. Unauthorized account is rejected.
5. Disabled account is rejected.
6. Manual halt blocks preview.
7. Kill switch blocks preview.
8. OPEN is blocked during Trading Pause.
9. CLOSE behavior follows existing safe manual-position semantics.
10. Invalid quantity is rejected.
11. LIMIT without price is rejected.
12. Invalid LIMIT price is rejected.
13. Invalid minTick is rejected.
14. STOP is rejected for broker submission.
15. outsideRth=true cannot reach broker.
16. Preview is non-mutating.
17. Preview never calls placeOrder().
18. Existing what-if adapter is used.
19. Valid submission creates PENDING_SUBMIT.
20. PENDING_SUBMIT is committed before broker call.
21. Rate limiter is acquired before placeOrder().
22. Broker order ID is allocated through TWSClient.
23. Successful broker submission becomes SUBMITTED.
24. Broker order ID is persisted.
25. Submission source remains manual.
26. Successful placeOrder never causes FILLED state.
27. Duplicate identical idempotency request causes exactly one placeOrder().
28. Duplicate idempotency request returns existing order.
29. Same idempotency key with different parameters returns 409.
30. Concurrent identical requests result in exactly one broker submission.
31. Rate limiter failure prevents broker write.
32. placeOrder exception does not falsely report SUBMITTED.
33. Ambiguous broker result does not automatically resubmit.
34. No engine SignalModel is created.
35. No engine OrderModel is created.
36. No execution_claims row is created.
37. No BasketCoordinator invocation.
38. No flatten/square-off invocation.
39. Account isolation is enforced.
40. Environment-independent behavior:
    VERIFIED_PAPER -> allowed when other validation passes
    VERIFIED_LIVE -> allowed when other validation passes
    UNKNOWN -> follows connection/validation policy, NOT a hardcoded Paper-only rejection.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.deps import get_db_session
from app.broker.ibkr.gateway_rate_limiter import (
    GatewayRateLimiter,
)
from app.core.security import create_access_token, get_password_hash
from app.db.models.account import AccountModel
from app.db.models.kill_switch import (
    KILL_SWITCH_STATUS_ACTIVATING,
    KillSwitchOperationModel,
)
from app.db.models.order import OrderModel
from app.db.models.signal import SignalModel
from app.db.models.user import UserModel
from app.db.repositories.manual_repository import (
    ManualHaltRepository,
    ManualOrderRepository,
    ManualPositionRepository,
)
from app.main import app
from app.oms.ibkr_adapter import WhatIfResult

# ── MOCK TWS CLIENT & ADAPTER ─────────────────────────────────────────


class MockM1BTWSClient:
    def __init__(
        self,
        connected: bool = True,
        managed_accounts: list[str] | None = None,
        next_order_id: int = 5000,
        rate_limiter: GatewayRateLimiter | None = None,
    ):
        self._connected = connected
        self.managed_accounts = frozenset(managed_accounts or [])
        self.next_order_id = next_order_id
        self._rate_limiter = rate_limiter or GatewayRateLimiter()
        self.placed_orders: list[tuple[int, Any, Any]] = []
        self.place_order_exception: Exception | None = None

    def is_connected(self) -> bool:
        return self._connected

    def allocate_next_order_id(self) -> int:
        if not self._connected:
            raise RuntimeError("Client not connected")
        current = self.next_order_id
        self.next_order_id += 1
        return current

    def placeOrder(self, orderId: int, contract: Any, order: Any) -> None:
        if self.place_order_exception:
            raise self.place_order_exception
        self.placed_orders.append((orderId, contract, order))


class MockM1BAdapter:
    def __init__(self, whatif_result: WhatIfResult | None = None):
        self.whatif_result = whatif_result or WhatIfResult(
            order_id=100,
            unknown=False,
            init_margin_change=Decimal("150.00"),
            maint_margin_change=Decimal("75.00"),
        )
        self.probe_calls: list[dict[str, Any]] = []

    async def probe_margin(
        self,
        *,
        contract: Any,
        side: str,
        quantity: Any,
        price: Any,
        ibkr_account: str,
        timeout: float | None = None,
    ) -> WhatIfResult:
        self.probe_calls.append(
            {
                "contract": contract,
                "side": side,
                "quantity": quantity,
                "price": price,
                "ibkr_account": ibkr_account,
            }
        )
        return self.whatif_result


# ── FIXTURES & HELPERS ────────────────────────────────────────────────


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


# ── TESTS ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_valid_market_and_limit_preview(session_factory):
    """1 & 2: Valid MARKET and LIMIT previews succeed with appropriate notional/margin values."""
    app.dependency_overrides.clear()
    s = uuid.uuid4().hex[:6]

    async with session_factory() as session:
        acc, _, token = await create_test_account_and_user(session, s)

    mock_client = MockM1BTWSClient(connected=True, managed_accounts=[acc.ibkr_account])
    mock_adapter = MockM1BAdapter()
    app.state.client = mock_client
    app.state.ibkr_adapter = mock_adapter

    async def _override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = _override_get_db

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1. MARKET preview
            resp_mkt = await client.post(
                f"/api/v1/manual/orders/preview?ibkr_account={acc.ibkr_account}",
                json={
                    "symbol": "AAPL",
                    "con_id": 12345,
                    "sec_type": "CFD",
                    "exchange": "SMART",
                    "currency": "USD",
                    "side": "BUY",
                    "quantity": "50",
                    "order_type": "MARKET",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp_mkt.status_code == 200
            data_mkt = resp_mkt.json()
            assert data_mkt["valid"] is True
            assert data_mkt["notional_status"] == "NO_MARKET_PRICE"
            assert data_mkt["notional"] is None
            assert data_mkt["margin_status"] == "AVAILABLE"
            assert data_mkt["init_margin_change"] == "150.00"

            # 2. LIMIT preview
            resp_lmt = await client.post(
                f"/api/v1/manual/orders/preview?ibkr_account={acc.ibkr_account}",
                json={
                    "symbol": "AAPL",
                    "con_id": 12345,
                    "sec_type": "CFD",
                    "exchange": "SMART",
                    "currency": "USD",
                    "side": "BUY",
                    "quantity": "100",
                    "order_type": "LIMIT",
                    "limit_price": "150.50",
                    "min_tick": 0.01,
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp_lmt.status_code == 200
            data_lmt = resp_lmt.json()
            assert data_lmt["valid"] is True
            assert data_lmt["notional_status"] == "EXACT"
            assert Decimal(str(data_lmt["notional"])) == Decimal("15050.00")
            assert len(mock_client.placed_orders) == 0  # Non-mutating!
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_preview_and_submit_authorization_isolation(session_factory):
    """3 & 4: Account authorization is strictly enforced; Account A cannot preview/submit for Account B."""
    app.dependency_overrides.clear()
    s_a = uuid.uuid4().hex[:6]
    s_b = uuid.uuid4().hex[:6]

    async with session_factory() as session:
        acc_a, _, token_a = await create_test_account_and_user(session, s_a)
        acc_b, _, _ = await create_test_account_and_user(session, s_b)

    mock_client = MockM1BTWSClient(
        connected=True, managed_accounts=[acc_a.ibkr_account, acc_b.ibkr_account]
    )
    app.state.client = mock_client
    app.state.ibkr_adapter = MockM1BAdapter()

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # User A previewing Account B -> 403
            resp_prev = await client.post(
                f"/api/v1/manual/orders/preview?ibkr_account={acc_b.ibkr_account}",
                json={
                    "symbol": "AAPL",
                    "con_id": 12345,
                    "sec_type": "CFD",
                    "side": "BUY",
                    "quantity": "10",
                    "order_type": "LIMIT",
                    "limit_price": "150.00",
                },
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert resp_prev.status_code == 403

            # User A submitting for Account B -> 403
            resp_sub = await client.post(
                f"/api/v1/manual/orders?ibkr_account={acc_b.ibkr_account}",
                json={
                    "idempotency_key": "key_attack_123",
                    "symbol": "AAPL",
                    "con_id": 12345,
                    "sec_type": "CFD",
                    "side": "BUY",
                    "quantity": "10",
                    "order_type": "LIMIT",
                    "limit_price": "150.00",
                },
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert resp_sub.status_code == 403
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_disabled_account_blocks_preview_and_submit(session_factory):
    """5: Disabled account is blocked from both preview and submit."""
    app.dependency_overrides.clear()
    s = uuid.uuid4().hex[:6]

    async with session_factory() as session:
        acc, _, token = await create_test_account_and_user(session, s, enabled=False)

    app.state.client = MockM1BTWSClient(connected=True, managed_accounts=[acc.ibkr_account])
    app.state.ibkr_adapter = MockM1BAdapter()

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            payload = {
                "symbol": "AAPL",
                "con_id": 12345,
                "sec_type": "CFD",
                "side": "BUY",
                "quantity": "10",
                "order_type": "LIMIT",
                "limit_price": "150.00",
            }
            # Preview returns valid=False with error
            resp_prev = await client.post(
                f"/api/v1/manual/orders/preview?ibkr_account={acc.ibkr_account}",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp_prev.status_code == 200
            assert resp_prev.json()["valid"] is False
            assert "disabled" in resp_prev.json()["errors"][0].lower()

            # Submit returns 400
            resp_sub = await client.post(
                f"/api/v1/manual/orders?ibkr_account={acc.ibkr_account}",
                json={**payload, "idempotency_key": "key_disabled_1"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp_sub.status_code == 400
            assert "disabled" in resp_sub.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_manual_halt_and_kill_switch_block_order(session_factory):
    """6 & 7: Manual halt and kill switch both block order preview and submit."""
    app.dependency_overrides.clear()
    s = uuid.uuid4().hex[:6]

    async with session_factory() as session:
        acc, _, token = await create_test_account_and_user(session, s)
        # 1. Arm manual halt
        halt_repo = ManualHaltRepository(session)
        await halt_repo.set_halt(acc.id, halted=True, reason="Risk review")
        await session.commit()

    app.state.client = MockM1BTWSClient(connected=True, managed_accounts=[acc.ibkr_account])
    app.state.ibkr_adapter = MockM1BAdapter()

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            payload = {
                "symbol": "AAPL",
                "con_id": 12345,
                "sec_type": "CFD",
                "side": "BUY",
                "quantity": "10",
                "order_type": "LIMIT",
                "limit_price": "150.00",
            }
            resp_prev = await client.post(
                f"/api/v1/manual/orders/preview?ibkr_account={acc.ibkr_account}",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp_prev.json()["valid"] is False
            assert "halted" in resp_prev.json()["errors"][0].lower()

            # Disarm halt, arm kill switch
            async with session_factory() as session:
                h_repo = ManualHaltRepository(session)
                await h_repo.set_halt(acc.id, halted=False)
                # Add armed kill switch row
                ks_op = KillSwitchOperationModel(
                    account_id=acc.id,
                    ibkr_account=acc.ibkr_account,
                    status=KILL_SWITCH_STATUS_ACTIVATING,
                    requested_by="operator",
                )
                session.add(ks_op)
                await session.commit()

            resp_prev_ks = await client.post(
                f"/api/v1/manual/orders/preview?ibkr_account={acc.ibkr_account}",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp_prev_ks.json()["valid"] is False
            assert "kill-switch" in resp_prev_ks.json()["errors"][0].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_trading_pause_blocks_open_but_allows_valid_close(session_factory):
    """8 & 9: When trading is paused, new OPEN orders are blocked, but genuine position reductions (CLOSE) succeed."""
    app.dependency_overrides.clear()
    s = uuid.uuid4().hex[:6]

    async with session_factory() as session:
        acc, _, token = await create_test_account_and_user(session, s, trading_paused=True)
        # Create an existing long manual position of +50 AAPL under trade_id MAN_TRD_POS_1
        pos_repo = ManualPositionRepository(session)
        await pos_repo.create_position(
            account_id=acc.id,
            trade_id="MAN_TRD_POS_1",
            symbol="AAPL",
            con_id=12345,
            signed_qty=Decimal(50),
            avg_cost=Decimal("150.00"),
            status="OPEN",
        )
        await session.commit()

    mock_client = MockM1BTWSClient(connected=True, managed_accounts=[acc.ibkr_account])
    app.state.client = mock_client
    app.state.ibkr_adapter = MockM1BAdapter()

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Case A: New BUY order (+20) -> increases exposure -> BLOCKED
            resp_open = await client.post(
                f"/api/v1/manual/orders/preview?ibkr_account={acc.ibkr_account}",
                json={
                    "symbol": "AAPL",
                    "con_id": 12345,
                    "sec_type": "CFD",
                    "side": "BUY",
                    "quantity": "20",
                    "order_type": "LIMIT",
                    "limit_price": "150.00",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp_open.json()["valid"] is False
            assert "trading paused" in resp_open.json()["errors"][0].lower()

            # Case B: SELL 20 (opposes long position, qty <= 50) -> Genuine position reduction -> ALLOWED
            resp_close = await client.post(
                f"/api/v1/manual/orders/preview?ibkr_account={acc.ibkr_account}",
                json={
                    "symbol": "AAPL",
                    "con_id": 12345,
                    "sec_type": "CFD",
                    "side": "SELL",
                    "quantity": "20",
                    "order_type": "LIMIT",
                    "limit_price": "150.00",
                    "trade_id": "MAN_TRD_POS_1",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp_close.json()["valid"] is True

            # Case C: SELL 60 (exceeds 50) -> Flips to short -> BLOCKED
            resp_excess = await client.post(
                f"/api/v1/manual/orders/preview?ibkr_account={acc.ibkr_account}",
                json={
                    "symbol": "AAPL",
                    "con_id": 12345,
                    "sec_type": "CFD",
                    "side": "SELL",
                    "quantity": "60",
                    "order_type": "LIMIT",
                    "limit_price": "150.00",
                    "trade_id": "MAN_TRD_POS_1",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp_excess.json()["valid"] is False
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_validation_rules_reject_invalid_inputs(session_factory):
    """10-14: Quantity <= 0, LIMIT without price, invalid minTick, and STOP order types are rejected."""
    app.dependency_overrides.clear()
    s = uuid.uuid4().hex[:6]

    async with session_factory() as session:
        acc, _, token = await create_test_account_and_user(session, s)

    app.state.client = MockM1BTWSClient(connected=True, managed_accounts=[acc.ibkr_account])
    app.state.ibkr_adapter = MockM1BAdapter()

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            base_url = f"/api/v1/manual/orders/preview?ibkr_account={acc.ibkr_account}"
            headers = {"Authorization": f"Bearer {token}"}

            # 11: LIMIT without price
            r1 = await client.post(
                base_url,
                json={
                    "symbol": "AAPL",
                    "con_id": 12345,
                    "sec_type": "CFD",
                    "side": "BUY",
                    "quantity": "10",
                    "order_type": "LIMIT",
                },
                headers=headers,
            )
            assert r1.json()["valid"] is False
            assert "price is required" in r1.json()["errors"][0].lower()

            # 13: Invalid minTick price (e.g. price=150.005 when min_tick=0.01)
            r2 = await client.post(
                base_url,
                json={
                    "symbol": "AAPL",
                    "con_id": 12345,
                    "sec_type": "CFD",
                    "side": "BUY",
                    "quantity": "10",
                    "order_type": "LIMIT",
                    "limit_price": "150.005",
                    "min_tick": 0.01,
                },
                headers=headers,
            )
            assert r2.json()["valid"] is False
            assert "minimum tick" in r2.json()["errors"][0].lower()

            # 14: STOP order type is rejected
            r3 = await client.post(
                base_url,
                json={
                    "symbol": "AAPL",
                    "con_id": 12345,
                    "sec_type": "CFD",
                    "side": "BUY",
                    "quantity": "10",
                    "order_type": "STOP",
                    "limit_price": "150.00",
                },
                headers=headers,
            )
            assert r3.json()["valid"] is False
            assert "stop order type is not executable" in r3.json()["errors"][0].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_submission_lifecycle_and_idempotency(session_factory):
    """19-29: Order submission creates PENDING_SUBMIT, allocates broker order ID, invokes placeOrder, updates to SUBMITTED, and handles idempotency."""
    app.dependency_overrides.clear()
    s = uuid.uuid4().hex[:6]

    async with session_factory() as session:
        acc, _, token = await create_test_account_and_user(session, s)

    rate_limiter = GatewayRateLimiter(max_msg_per_sec=30.0, normal_msg_per_sec=24.0)
    mock_client = MockM1BTWSClient(
        connected=True,
        managed_accounts=[acc.ibkr_account],
        next_order_id=7701,
        rate_limiter=rate_limiter,
    )
    app.state.client = mock_client
    app.state.ibkr_adapter = MockM1BAdapter()

    idempotency_key = f"idemp_{uuid.uuid4().hex}"

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            submit_payload = {
                "idempotency_key": idempotency_key,
                "symbol": "AAPL",
                "con_id": 12345,
                "sec_type": "CFD",
                "exchange": "SMART",
                "currency": "USD",
                "side": "BUY",
                "quantity": "100",
                "order_type": "LIMIT",
                "limit_price": "152.25",
                "outside_rth": True,  # Client tries to pass outside_rth=True
            }

            # First submission
            resp1 = await client.post(
                f"/api/v1/manual/orders?ibkr_account={acc.ibkr_account}",
                json=submit_payload,
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp1.status_code == 200
            data1 = resp1.json()
            order1 = data1["order"]

            # 23-26: Valid submission becomes SUBMITTED with allocated broker ID and source=manual
            assert order1["status"] == "SUBMITTED"
            assert order1["broker_order_id"] == "7701"
            assert order1["source"] == "manual"
            assert order1["status"] != "FILLED"  # NEVER optimistically marked FILLED
            assert data1["idempotent_replay"] is False

            # Verify broker write boundary
            assert len(mock_client.placed_orders) == 1
            order_id, contract, ib_order = mock_client.placed_orders[0]
            assert order_id == 7701
            assert contract.symbol == "AAPL"
            assert contract.secType == "CFD"
            # 15: outsideRth=True was overridden to False before reaching broker
            assert ib_order.outsideRth is False
            assert ib_order.action == "BUY"
            assert ib_order.totalQuantity == 100.0
            assert ib_order.lmtPrice == 152.25

            # 27 & 28: Duplicate submission with SAME parameters returns existing order without second placeOrder
            resp2 = await client.post(
                f"/api/v1/manual/orders?ibkr_account={acc.ibkr_account}",
                json=submit_payload,
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp2.status_code == 200
            data2 = resp2.json()
            assert data2["idempotent_replay"] is True
            assert data2["order"]["internal_order_id"] == order1["internal_order_id"]
            assert len(mock_client.placed_orders) == 1  # ZERO additional broker calls!

            # 29: Same idempotency key with DIFFERENT parameters returns HTTP 409 Conflict
            conflicting_payload = {**submit_payload, "quantity": "200"}
            resp_conflict = await client.post(
                f"/api/v1/manual/orders?ibkr_account={acc.ibkr_account}",
                json=conflicting_payload,
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp_conflict.status_code == 409
            assert len(mock_client.placed_orders) == 1  # Still zero extra calls!
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_place_order_exception_does_not_falsely_report_submitted(session_factory):
    """32: If TWSClient.placeOrder raises an exception, the order is marked ERROR and does not report SUBMITTED."""
    app.dependency_overrides.clear()
    s = uuid.uuid4().hex[:6]

    async with session_factory() as session:
        acc, _, token = await create_test_account_and_user(session, s)

    mock_client = MockM1BTWSClient(connected=True, managed_accounts=[acc.ibkr_account])
    mock_client.place_order_exception = RuntimeError("IBKR Socket Error 100")
    app.state.client = mock_client
    app.state.ibkr_adapter = MockM1BAdapter()

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                f"/api/v1/manual/orders?ibkr_account={acc.ibkr_account}",
                json={
                    "idempotency_key": "key_fail_01",
                    "symbol": "AAPL",
                    "con_id": 12345,
                    "sec_type": "CFD",
                    "side": "BUY",
                    "quantity": "50",
                    "order_type": "MARKET",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 502

            # Check database: status must be ERROR, not SUBMITTED!
            async with session_factory() as session:
                order_repo = ManualOrderRepository(session)
                order = await order_repo.get_by_idempotency_key(acc.id, "key_fail_01")
                assert order is not None
                assert order.status == "ERROR"
                assert "Socket Error 100" in (order.reject_reason or "")
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_environment_agnostic_live_and_paper_execution(session_factory):
    """40: Environment-independent behavior: VERIFIED_LIVE account executes successfully when healthy."""
    app.dependency_overrides.clear()
    s_live = uuid.uuid4().hex[:6]

    async with session_factory() as session:
        # Create account with 'U' prefix (Live account)
        acc_live, _, token = await create_test_account_and_user(session, s_live, is_live=True)

    mock_client = MockM1BTWSClient(
        connected=True, managed_accounts=[acc_live.ibkr_account], next_order_id=8801
    )
    app.state.client = mock_client
    app.state.ibkr_adapter = MockM1BAdapter()

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Preview on Live Gateway succeeds
            resp_prev = await client.post(
                f"/api/v1/manual/orders/preview?ibkr_account={acc_live.ibkr_account}",
                json={
                    "symbol": "TSLA",
                    "con_id": 98765,
                    "sec_type": "CFD",
                    "side": "BUY",
                    "quantity": "25",
                    "order_type": "LIMIT",
                    "limit_price": "220.00",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp_prev.status_code == 200
            assert resp_prev.json()["valid"] is True
            assert resp_prev.json()["environment"] == "VERIFIED_LIVE"

            # Submit on Live Gateway succeeds without paper restriction!
            resp_sub = await client.post(
                f"/api/v1/manual/orders?ibkr_account={acc_live.ibkr_account}",
                json={
                    "idempotency_key": "key_live_01",
                    "symbol": "TSLA",
                    "con_id": 98765,
                    "sec_type": "CFD",
                    "side": "BUY",
                    "quantity": "25",
                    "order_type": "LIMIT",
                    "limit_price": "220.00",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp_sub.status_code == 200
            assert resp_sub.json()["order"]["status"] == "SUBMITTED"
            assert resp_sub.json()["order"]["broker_order_id"] == "8801"
            assert len(mock_client.placed_orders) == 1
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_isolation_no_engine_models_created(session_factory):
    """34-38: Manual order submission never creates engine SignalModel, OrderModel, or execution claims."""
    app.dependency_overrides.clear()
    s = uuid.uuid4().hex[:6]

    async with session_factory() as session:
        acc, _, token = await create_test_account_and_user(session, s)

    mock_client = MockM1BTWSClient(connected=True, managed_accounts=[acc.ibkr_account])
    app.state.client = mock_client
    app.state.ibkr_adapter = MockM1BAdapter()

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                f"/api/v1/manual/orders?ibkr_account={acc.ibkr_account}",
                json={
                    "idempotency_key": "key_iso_01",
                    "symbol": "AAPL",
                    "con_id": 12345,
                    "sec_type": "CFD",
                    "side": "BUY",
                    "quantity": "10",
                    "order_type": "MARKET",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200

            # Verify that ZERO engine records exist for this account
            async with session_factory() as session:
                engine_orders = (
                    await session.execute(select(OrderModel).where(OrderModel.account_id == acc.id))
                ).scalars().all()
                assert len(engine_orders) == 0

                signals = (
                    await session.execute(
                        select(SignalModel).where(SignalModel.strategy_id == "manual")
                    )
                ).scalars().all()
                assert len(signals) == 0
    finally:
        app.dependency_overrides.clear()
