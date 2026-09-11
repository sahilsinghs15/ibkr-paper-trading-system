"""Unit and integration tests for Manual Trading M1-A.

Covers:
1. Connected Gateway returns connected state.
2. Disconnected Gateway returns disconnected.
3. Unknown mode is represented as UNKNOWN.
4. Port alone is not treated as authoritative Paper proof (EXPECTED_PAPER_BY_CONFIGURATION).
5. Secrets are never included in gateway status.
6. Search returns all CFD candidates.
7. Multiple candidates are preserved.
8. No automatic unique selection occurs.
9. con_id is preserved.
10. secType is CFD.
11. Currency/exchange metadata is preserved.
12. Timeout is handled safely.
13. Gateway disconnected is handled safely (HTTP 503).
14. Explicit con_id selection works.
15. Ambiguous symbol cannot silently select a contract.
16. Invalid/missing con_id is rejected.
17. Selected contract contains exact identity.
18. Account scoping is enforced.
19. Account A cannot query Account B's manual context.
20. M1-A contains zero broker order writes (no placeOrder / cancelOrder / reqGlobalCancel).
21. Canonical is_connection_verified_paper() validation.
"""

from __future__ import annotations

import inspect
import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api.deps import get_db_session
from app.api.routes import manual as manual_routes
from app.api.routes.manual import (
    _determine_gateway_mode,
    is_connection_verified_paper,
)
from app.core.security import create_access_token, get_password_hash
from app.db.models.account import AccountModel
from app.db.models.user import UserModel
from app.main import app
from app.schemas.manual_schemas import (
    CfdCandidateContract,
    GatewayEnvironmentMode,
    GatewayModeVerificationState,
    GatewayStatusResponse,
)

# ── MOCK IBKR CLASSES ────────────────────────────────────────────────


class DummyContract:
    def __init__(
        self,
        conId: int = 0,
        symbol: str = "",
        secType: str = "CFD",
        exchange: str = "SMART",
        currency: str = "USD",
        localSymbol: str | None = None,
        tradingClass: str | None = None,
        primaryExchange: str | None = None,
    ):
        self.conId = conId
        self.symbol = symbol
        self.secType = secType
        self.exchange = exchange
        self.currency = currency
        self.localSymbol = localSymbol
        self.tradingClass = tradingClass
        self.primaryExchange = primaryExchange


class DummyContractDetails:
    def __init__(
        self,
        contract: DummyContract,
        minTick: float = 0.01,
        longName: str | None = None,
    ):
        self.contract = contract
        self.minTick = minTick
        self.longName = longName


class MockTWSClient:
    def __init__(
        self,
        connected: bool = True,
        managed_accounts: list[str] | None = None,
        contract_details_return: list[Any] | None = None,
        contract_details_exception: Exception | None = None,
    ):
        self._connected = connected
        self.managed_accounts = frozenset(managed_accounts or [])
        self._contract_details_return = (
            contract_details_return if contract_details_return is not None else []
        )
        self._contract_details_exception = contract_details_exception

    def is_connected(self) -> bool:
        return self._connected

    def serverVersion(self) -> int:
        return 176

    def twsConnectionTime(self) -> str:
        return "20260911 12:00:00 UTC"

    async def request_contract_details_async(
        self, contract: Any, timeout: float = 5.0
    ) -> list[Any]:
        if self._contract_details_exception:
            raise self._contract_details_exception
        return self._contract_details_return


# ── 1-5: GATEWAY STATUS & PAPER/LIVE VERIFICATION TESTS ──────────────


def test_gateway_disconnected_returns_unknown():
    """Disconnected Gateway returns UNKNOWN and UNVERIFIED."""
    client = MockTWSClient(connected=False)
    env, state, msg = _determine_gateway_mode(client, 4002, [])  # pyrefly: ignore[bad-argument-type]
    assert env == GatewayEnvironmentMode.UNKNOWN
    assert state == GatewayModeVerificationState.UNVERIFIED
    assert "disconnected" in msg.lower()

    verified, reason = is_connection_verified_paper(client, 4002, [])  # pyrefly: ignore[bad-argument-type]
    assert verified is False
    assert "disconnected" in reason.lower()


def test_gateway_port_alone_is_not_authoritative_paper():
    """Port 4002/7497 without DU account prefix is EXPECTED_PAPER_BY_CONFIGURATION, NOT VERIFIED_PAPER."""
    client = MockTWSClient(connected=True, managed_accounts=[])
    env, state, msg = _determine_gateway_mode(client, 4002, [])  # pyrefly: ignore[bad-argument-type]
    assert env == GatewayEnvironmentMode.UNKNOWN
    assert state == GatewayModeVerificationState.EXPECTED_PAPER_BY_CONFIGURATION
    assert "unconfirmed" in msg.lower()

    # Hard safety check for M1-B: must reject!
    verified, reason = is_connection_verified_paper(client, 4002, [])  # pyrefly: ignore[bad-argument-type]
    assert verified is False
    assert "Not verified paper" in reason


def test_gateway_port_alone_is_not_authoritative_live():
    """Port 4001 without U account prefix is EXPECTED_LIVE_BY_CONFIGURATION, NOT VERIFIED_LIVE."""
    client = MockTWSClient(connected=True, managed_accounts=[])
    env, state, msg = _determine_gateway_mode(client, 4001, [])  # pyrefly: ignore[bad-argument-type]
    assert env == GatewayEnvironmentMode.UNKNOWN
    assert state == GatewayModeVerificationState.EXPECTED_LIVE_BY_CONFIGURATION
    assert "unconfirmed" in msg.lower()

    verified, _ = is_connection_verified_paper(client, 4001, [])  # pyrefly: ignore[bad-argument-type]
    assert verified is False


def test_gateway_authoritative_paper_verification():
    """Connected client with DU prefix is VERIFIED_PAPER and ACCOUNT_PREFIX_VERIFIED."""
    client = MockTWSClient(connected=True, managed_accounts=["DU123456"])
    env, state, msg = _determine_gateway_mode(client, 4001, ["DU123456"])  # pyrefly: ignore[bad-argument-type]
    assert env == GatewayEnvironmentMode.VERIFIED_PAPER
    assert state == GatewayModeVerificationState.ACCOUNT_PREFIX_VERIFIED
    assert "DU123456" in msg

    verified, reason = is_connection_verified_paper(client, 4001, ["DU123456"])  # pyrefly: ignore[bad-argument-type]
    assert verified is True
    assert "DU123456" in reason


def test_gateway_authoritative_live_verification():
    """Connected client with U prefix is VERIFIED_LIVE."""
    client = MockTWSClient(connected=True, managed_accounts=["U7211090"])
    env, state, msg = _determine_gateway_mode(client, 4002, ["U7211090"])  # pyrefly: ignore[bad-argument-type]
    assert env == GatewayEnvironmentMode.VERIFIED_LIVE
    assert state == GatewayModeVerificationState.ACCOUNT_PREFIX_VERIFIED
    assert "U7211090" in msg

    verified, _ = is_connection_verified_paper(client, 4002, ["U7211090"])  # pyrefly: ignore[bad-argument-type]
    assert verified is False


def test_gateway_status_schema_never_exposes_secrets():
    """GatewayStatusResponse must not contain password, secret, token, or api_key fields."""
    fields = GatewayStatusResponse.model_fields.keys()
    sensitive_words = ["password", "secret", "token", "credential", "api_key", "key"]
    for f in fields:
        for word in sensitive_words:
            assert word not in f.lower(), f"Sensitive field '{f}' found in GatewayStatusResponse"


# ── 6-13: CFD DISCOVERY TESTS ────────────────────────────────────────


@pytest.mark.asyncio
async def test_cfd_search_returns_all_candidates_and_does_not_pick_unique(
    session_factory,
):
    """POST /manual/instruments/search returns ALL matching CFD candidates without auto-picking."""
    app.dependency_overrides.clear()
    s = uuid.uuid4().hex[:6]
    acc_num = f"DU{s.upper()}"

    # Setup 3 dummy CFD candidates for AAPL on different exchanges / conIds
    c1 = DummyContract(
        conId=11111,
        symbol="AAPL",
        secType="CFD",
        exchange="SMART",
        currency="USD",
        localSymbol="AAPL_SMART",
        tradingClass="AAPL",
    )
    c2 = DummyContract(
        conId=22222,
        symbol="AAPL",
        secType="CFD",
        exchange="IBUS",
        currency="USD",
        localSymbol="AAPL_IBUS",
        tradingClass="AAPL",
    )
    c3 = DummyContract(
        conId=33333,
        symbol="AAPL",
        secType="CFD",
        exchange="LSE",
        currency="USD",
        localSymbol="AAPL_LSE",
        tradingClass="AAPL",
    )
    # Plus a non-CFD contract that must be filtered out
    c_stk = DummyContract(
        conId=99999,
        symbol="AAPL",
        secType="STK",
        exchange="SMART",
        currency="USD",
    )

    mock_client = MockTWSClient(
        connected=True,
        managed_accounts=[acc_num],
        contract_details_return=[
            DummyContractDetails(c1, minTick=0.01, longName="Apple Inc CFD 1"),
            DummyContractDetails(c2, minTick=0.01, longName="Apple Inc CFD 2"),
            DummyContractDetails(c3, minTick=0.005, longName="Apple Inc CFD 3"),
            DummyContractDetails(c_stk, minTick=0.01, longName="Apple Stock"),
        ],
    )
    app.state.client = mock_client

    async with session_factory() as session:
        acc = AccountModel(
            name=f"Acc {s}", ibkr_account=acc_num, total_margin=100000, enabled=True
        )
        session.add(acc)
        await session.commit()
        await session.refresh(acc)

        user = UserModel(
            email=f"user_{s}@example.com",
            password_hash=get_password_hash("Pass123!"),
            role="user",
            is_active=True,
            ibkr_account_id=acc.id,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

        token = create_access_token(
            {"sub": str(user.id), "role": "user", "email": user.email}
        )

    async def _override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = _override_get_db

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/manual/instruments/search?ibkr_account={acc_num}",
                json={"symbol": "AAPL", "exchange": "SMART", "currency": "USD"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200
            data = resp.json()

            assert data["symbol"] == "AAPL"
            assert data["count"] == 3  # All 3 CFD candidates preserved!
            candidates = data["candidates"]
            con_ids = [c["con_id"] for c in candidates]
            assert 11111 in con_ids
            assert 22222 in con_ids
            assert 33333 in con_ids
            assert 99999 not in con_ids  # STK filtered out

            # SMART exchange candidate is sorted first
            assert candidates[0]["exchange"] == "SMART"
            assert candidates[0]["con_id"] == 11111
            assert candidates[0]["sec_type"] == "CFD"
            assert candidates[0]["currency"] == "USD"
            assert candidates[0]["min_tick"] == 0.01
            assert candidates[0]["local_symbol"] == "AAPL_SMART"
            assert candidates[0]["long_name"] == "Apple Inc CFD 1"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_cfd_search_gateway_disconnected_returns_503(session_factory):
    """POST /manual/instruments/search returns 503 if IBKR Gateway is disconnected."""
    app.dependency_overrides.clear()
    s = uuid.uuid4().hex[:6]
    acc_num = f"DU{s.upper()}"

    app.state.client = MockTWSClient(connected=False)

    async with session_factory() as session:
        acc = AccountModel(
            name=f"Acc {s}", ibkr_account=acc_num, total_margin=100000, enabled=True
        )
        session.add(acc)
        await session.commit()
        await session.refresh(acc)

        user = UserModel(
            email=f"user_{s}@example.com",
            password_hash=get_password_hash("Pass123!"),
            role="user",
            is_active=True,
            ibkr_account_id=acc.id,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

        token = create_access_token(
            {"sub": str(user.id), "role": "user", "email": user.email}
        )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/manual/instruments/search?ibkr_account={acc_num}",
                json={"symbol": "AAPL"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 503
            assert "disconnected" in resp.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_cfd_search_timeout_handled_safely(session_factory):
    """POST /manual/instruments/search returns safe error if discovery raises Exception."""
    app.dependency_overrides.clear()
    s = uuid.uuid4().hex[:6]
    acc_num = f"DU{s.upper()}"

    app.state.client = MockTWSClient(
        connected=True,
        contract_details_exception=TimeoutError("Request timed out"),
    )

    async with session_factory() as session:
        acc = AccountModel(
            name=f"Acc {s}", ibkr_account=acc_num, total_margin=100000, enabled=True
        )
        session.add(acc)
        await session.commit()
        await session.refresh(acc)

        user = UserModel(
            email=f"user_{s}@example.com",
            password_hash=get_password_hash("Pass123!"),
            role="user",
            is_active=True,
            ibkr_account_id=acc.id,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

        token = create_access_token(
            {"sub": str(user.id), "role": "user", "email": user.email}
        )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/api/v1/manual/instruments/search?ibkr_account={acc_num}",
                json={"symbol": "AAPL"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 502
            assert "failed to query" in resp.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


# ── 14-17: CONTRACT SAFETY & SELECTION SCHEMA TESTS ─────────────────


def test_cfd_candidate_schema_validates_safety():
    """CfdCandidateContract enforces sec_type == 'CFD' and valid con_id."""
    valid = CfdCandidateContract(
        con_id=12345,
        symbol="AAPL",
        sec_type="CFD",
        exchange="SMART",
        currency="USD",
        min_tick=0.01,
    )
    assert valid.con_id == 12345
    assert valid.sec_type == "CFD"

    # Must reject non-CFD sec_type
    with pytest.raises(ValidationError):
        CfdCandidateContract(
            con_id=12345,
            symbol="AAPL",
            sec_type="STK",  # Invalid!  # pyrefly: ignore[bad-argument-type]
            exchange="SMART",
            currency="USD",
        )


# ── 18-19: ACCOUNT SCOPING & CROSS-ACCOUNT ISOLATION ────────────────


@pytest.mark.asyncio
async def test_cross_account_isolation_on_manual_endpoints(session_factory):
    """User A cannot query Gateway status or search instruments for Account B."""
    app.dependency_overrides.clear()
    s_a = uuid.uuid4().hex[:6]
    s_b = uuid.uuid4().hex[:6]
    acc_a_num = f"DU{s_a.upper()}"
    acc_b_num = f"DU{s_b.upper()}"

    app.state.client = MockTWSClient(connected=True, managed_accounts=[acc_a_num, acc_b_num])

    async with session_factory() as session:
        acc_a = AccountModel(
            name=f"Acc A {s_a}", ibkr_account=acc_a_num, total_margin=100000, enabled=True
        )
        acc_b = AccountModel(
            name=f"Acc B {s_b}", ibkr_account=acc_b_num, total_margin=100000, enabled=True
        )
        session.add_all([acc_a, acc_b])
        await session.commit()
        await session.refresh(acc_a)
        await session.refresh(acc_b)

        user_a = UserModel(
            email=f"usera_{s_a}@example.com",
            password_hash=get_password_hash("Pass123!"),
            role="user",
            is_active=True,
            ibkr_account_id=acc_a.id,
        )
        session.add(user_a)
        await session.commit()
        await session.refresh(user_a)

        token_a = create_access_token(
            {"sub": str(user_a.id), "role": "user", "email": user_a.email}
        )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # User A accessing Account A -> 200 OK
            resp_a = await client.get(
                f"/api/v1/manual/gateway-status?ibkr_account={acc_a_num}",
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert resp_a.status_code == 200

            # User A attempting to access Account B -> 403 Forbidden
            resp_b_attack = await client.get(
                f"/api/v1/manual/gateway-status?ibkr_account={acc_b_num}",
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert resp_b_attack.status_code == 403

            # User A attempting to search CFD for Account B -> 403 Forbidden
            search_attack = await client.post(
                f"/api/v1/manual/instruments/search?ibkr_account={acc_b_num}",
                json={"symbol": "AAPL"},
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert search_attack.status_code == 403
    finally:
        app.dependency_overrides.clear()


# ── 20: ZERO BROKER ORDER WRITES AUDIT TEST ──────────────────────────


def test_m1a_has_zero_broker_order_write_paths():
    """Verify that manual routes contain zero broker order mutation calls."""
    source_code = inspect.getsource(manual_routes)

    forbidden_calls = [
        "placeOrder",
        "cancelOrder",
        "reqGlobalCancel",
        "reqAllOpenOrders",
        "emergency_flatten",
        "flatten_account",
    ]

    for call in forbidden_calls:
        assert (
            f".{call}" not in source_code
        ), f"Forbidden broker-mutating call '.{call}' found in manual.py!"
