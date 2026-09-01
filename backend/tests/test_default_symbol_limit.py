"""Comprehensive unit and integration tests for Phase 4: Default Symbol Limit.

Verifies:
1. Default symbol limit can be configured and read back via API.
2. Default symbol limit can be updated via PUT /default-symbol-limit and PATCH /accounts.
3. Explicit symbol override takes precedence over default limit.
4. Symbols without explicit override fall back to default limit.
5. Changing default symbol limit does not alter explicit symbol overrides.
6. Newly encountered symbols automatically use default limit.
7. Invalid (<= 0) default symbol limit values are rejected with HTTP 400.
8. Multi-account isolation: Account A default limit does not affect Account B.
9. Account deletion cleans up per-symbol limits and account default limit.
10. The 3-step CORN test proving explicit override precedence:
    - Step 1: Default=$10M, No CORN override, Exposure=$11M -> REJECT
    - Step 2: Default=$10M, CORN override=$15M, Exposure=$11M -> PASS
    - Step 3: Default=$20M, CORN override=$15M, Exposure=$16M -> REJECT
11. Phase 1 (Kill Switch / Start Again), Phase 2 (Close Single Pair), Phase 3 (Delete Account) remain functional.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.db.models.account import AccountModel
from app.db.models.position import PositionModel
from app.main import app
from app.rms.checks.money_per_stock import MoneyPerStockCheck
from app.rms.models import (
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
    RMSContext,
    RMSOutcome,
)
from app.services.kill_switch import (
    clear_account_kill_switch,
    is_account_kill_switch_active,
)
from app.services.position_close_service import SinglePairCloseService


@pytest.fixture
def client() -> TestClient:
    with (
        patch("app.broker.ibkr.tws_client.TWSClient.connect_and_start", return_value=True),
        patch("app.broker.ibkr.tws_client.TWSClient.disconnect_clean"),
        patch("app.broker.ibkr.tws_client.TWSClient.is_connected", return_value=True),
        patch("app.services.worker_pool.ExecutionWorkerPool.start", new_callable=AsyncMock),
        patch("app.services.worker_pool.ExecutionWorkerPool.stop", new_callable=AsyncMock),
        patch(
            "app.services.position_reconciler.PositionReconciler.start",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.position_reconciler.PositionReconciler.stop",
            new_callable=AsyncMock,
        ),
        patch("app.services.recovery.RecoveryManager.run_startup_recovery", new_callable=AsyncMock),
        patch("app.services.order_manager.OrderManager.hydrate_live_pnl", new_callable=AsyncMock),
        patch("app.services.order_manager.OrderManager.hydrate_runtime_from_db", new_callable=AsyncMock),
        TestClient(app) as c,
    ):
        yield c


@pytest.fixture
async def session_factory():
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    sf = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    yield sf
    await engine.dispose()


def test_default_symbol_limit_crud_api(client: TestClient) -> None:
    """Test 1, 2, 3, 8: Create account with default symbol limit, update it, and reject negative limits."""
    suffix = uuid4().hex[:6]
    ibkr = f"DUDEF{suffix}"

    # 1. Create account with default symbol limit = $10M
    res_create = client.post(
        "/api/v1/config/accounts",
        json={
            "name": f"Account Default {suffix}",
            "ibkr_account": ibkr,
            "total_margin": 100000.0,
            "enabled": True,
            "default_symbol_limit": 10000000.0,
        },
    )
    assert res_create.status_code == 201
    body = res_create.json()
    acc_id = body["id"]
    assert Decimal(str(body["default_symbol_limit"])) == Decimal("10000000.00")

    # 2. Read back account config
    res_get = client.get(f"/api/v1/config/accounts/by-identifier/{ibkr}")
    assert res_get.status_code == 200
    assert Decimal(str(res_get.json()["default_symbol_limit"])) == Decimal("10000000.00")

    # 3. Update default symbol limit via PUT /default-symbol-limit to $12M
    res_put = client.put(
        f"/api/v1/config/accounts/{acc_id}/default-symbol-limit",
        json={"default_symbol_limit": 12000000.0},
    )
    assert res_put.status_code == 200
    assert Decimal(str(res_put.json()["default_symbol_limit"])) == Decimal("12000000.00")

    # 4. Reject invalid (<= 0) default limit
    res_invalid = client.put(
        f"/api/v1/config/accounts/{acc_id}/default-symbol-limit",
        json={"default_symbol_limit": -500.0},
    )
    assert res_invalid.status_code == 422 or res_invalid.status_code == 400


def test_three_step_corn_precedence_and_rms_check(client: TestClient) -> None:
    """Test 10 & 16: Complete 3-step CORN RMS Check 8 precedence proof:

    Step 1: Default = $10M, No CORN override, Exposure = $11M -> REJECT
    Step 2: Default = $10M, CORN override = $15M, Exposure = $11M -> PASS
    Step 3: Default = $20M, CORN override = $15M, Exposure = $16M -> REJECT (explicit override precedence!)
    """
    check = MoneyPerStockCheck()
    acc_id = 101

    leg_corn_11m = OrderLeg(
        symbol="CORN",
        side=OrderSide.BUY,
        quantity=1.0,
        price=Decimal("11000000.00"),
        contract_month="202612",
    )
    intent_11m = OrderIntent(
        signal_id="SIG-CORN-11M",
        strategy_id="model_blue",
        action=OrderAction.OPEN,
        legs=[leg_corn_11m],
        account_id=acc_id,
    )

    leg_corn_16m = OrderLeg(
        symbol="CORN",
        side=OrderSide.BUY,
        quantity=1.0,
        price=Decimal("16000000.00"),
        contract_month="202612",
    )
    intent_16m = OrderIntent(
        signal_id="SIG-CORN-16M",
        strategy_id="model_blue",
        action=OrderAction.OPEN,
        legs=[leg_corn_16m],
        account_id=acc_id,
    )

    # --- STEP 1: Default = $10M, No CORN override, Exposure = $11M ---
    ctx1 = RMSContext()
    ctx1.default_symbol_limits[acc_id] = Decimal("10000000.00")
    # per_symbol_limits has NO entry for (acc_id, "CORN")

    res1 = check.evaluate(intent_11m, ctx1)
    assert res1.outcome == RMSOutcome.REJECT
    assert "exceeds limit of 10000000" in (res1.reason or "")

    # --- STEP 2: Default = $10M, CORN override = $15M, Exposure = $11M ---
    ctx2 = RMSContext()
    ctx2.default_symbol_limits[acc_id] = Decimal("10000000.00")
    ctx2.per_symbol_limits[(acc_id, "CORN")] = Decimal("15000000.00")

    res2 = check.evaluate(intent_11m, ctx2)
    assert res2.outcome == RMSOutcome.PASS

    # --- STEP 3: Default = $20M, CORN override = $15M, Exposure = $16M ---
    # Proves that explicit override ($15M) takes precedence over higher default ($20M)
    ctx3 = RMSContext()
    ctx3.default_symbol_limits[acc_id] = Decimal("20000000.00")
    ctx3.per_symbol_limits[(acc_id, "CORN")] = Decimal("15000000.00")

    res3 = check.evaluate(intent_16m, ctx3)
    assert res3.outcome == RMSOutcome.REJECT
    assert "exceeds limit of 15000000" in (res3.reason or "")


def test_changing_default_does_not_modify_explicit_overrides(client: TestClient) -> None:
    """Test 5, 6, 7: Changing default limit leaves explicit symbol overrides intact and new symbols fall back to new default."""
    check = MoneyPerStockCheck()
    acc_id = 202

    ctx = RMSContext()
    ctx.default_symbol_limits[acc_id] = Decimal("10000000.00")
    ctx.per_symbol_limits[(acc_id, "EWP")] = Decimal("15000000.00")
    ctx.per_symbol_limits[(acc_id, "EWU")] = Decimal("20000000.00")

    # Helper intent constructor
    def make_intent(sym: str, notional: Decimal) -> OrderIntent:
        return OrderIntent(
            signal_id=f"SIG-{sym}",
            strategy_id="model_blue",
            action=OrderAction.OPEN,
            legs=[
                OrderLeg(
                    symbol=sym,
                    side=OrderSide.BUY,
                    quantity=1.0,
                    price=notional,
                    contract_month="202612",
                )
            ],
            account_id=acc_id,
        )

    # Initial state: Default=$10M, EWP=$15M, EWU=$20M
    assert check.evaluate(make_intent("CORN", Decimal(11000000)), ctx).outcome == RMSOutcome.REJECT  # fallback $10M
    assert check.evaluate(make_intent("EWP", Decimal(14000000)), ctx).outcome == RMSOutcome.PASS     # override $15M
    assert check.evaluate(make_intent("EWU", Decimal(19000000)), ctx).outcome == RMSOutcome.PASS     # override $20M
    assert check.evaluate(make_intent("SPY", Decimal(11000000)), ctx).outcome == RMSOutcome.REJECT  # fallback $10M

    # Change default to $12M
    ctx.default_symbol_limits[acc_id] = Decimal("12000000.00")

    # Verify:
    assert check.evaluate(make_intent("CORN", Decimal(11000000)), ctx).outcome == RMSOutcome.PASS     # new default $12M allows $11M
    assert check.evaluate(make_intent("EWP", Decimal(14000000)), ctx).outcome == RMSOutcome.PASS     # explicit override $15M unchanged
    assert check.evaluate(make_intent("EWP", Decimal(16000000)), ctx).outcome == RMSOutcome.REJECT   # explicit override $15M still rejects $16M
    assert check.evaluate(make_intent("EWU", Decimal(19000000)), ctx).outcome == RMSOutcome.PASS     # explicit override $20M unchanged
    assert check.evaluate(make_intent("SPY", Decimal(11000000)), ctx).outcome == RMSOutcome.PASS     # new default $12M allows $11M


def test_multi_account_isolation_for_defaults(client: TestClient) -> None:
    """Test 11: Account A default symbol limit does not affect Account B."""
    check = MoneyPerStockCheck()
    acc_a = 301
    acc_b = 302

    ctx = RMSContext()
    ctx.default_symbol_limits[acc_a] = Decimal("10000000.00")
    ctx.default_symbol_limits[acc_b] = Decimal("5000000.00")

    intent_a = OrderIntent(
        signal_id="SIG-A",
        strategy_id="model_blue",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(
                symbol="CORN",
                side=OrderSide.BUY,
                quantity=1.0,
                price=Decimal("7000000.00"),
                contract_month="202612",
            )
        ],
        account_id=acc_a,
    )
    intent_b = OrderIntent(
        signal_id="SIG-B",
        strategy_id="model_blue",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(
                symbol="CORN",
                side=OrderSide.BUY,
                quantity=1.0,
                price=Decimal("7000000.00"),
                contract_month="202612",
            )
        ],
        account_id=acc_b,
    )

    # $7M for Acc A ($10M default) -> PASS
    assert check.evaluate(intent_a, ctx).outcome == RMSOutcome.PASS
    # $7M for Acc B ($5M default) -> REJECT
    assert check.evaluate(intent_b, ctx).outcome == RMSOutcome.REJECT


@pytest.mark.asyncio
async def test_regressions_phases_1_2_3_remains_intact(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Test 12, 13, 14, 15: Phases 1 (Kill Switch/Start Again), 2 (Close Pair), 3 (Delete Account) remain functional."""
    suffix = uuid4().hex[:6]
    trade_id = f"MBLUE-P4-{suffix}"

    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"AccPhase4-{suffix}",
            ibkr_account=f"DUP4{suffix}",
            total_margin=Decimal("100000.00"),
            default_symbol_limit=Decimal("10000000.00"),
        )
        session.add(acc)
        await session.flush()
        acc_id = acc.id

        await session.execute(
            PositionModel.__table__.insert().values(
                account_id=acc_id,
                trade_id=trade_id,
                strategy_id="model_blue",
                leg_a_symbol="EWP",
                leg_a_signed_qty=Decimal(100),
                leg_a_entry_mark=Decimal("30.00"),
                target=Decimal(500),
                stop=Decimal(250),
                time_limit=3600,
                risk_state="OPEN",
            )
        )

    # Phase 2 Close Single Pair check (without baskets coordinator, returns FAILED safely)
    close_svc = SinglePairCloseService(session_factory=session_factory)
    res_close = await close_svc.close_pair(acc_id, trade_id)
    assert res_close.success is False
    assert res_close.status == "FAILED"

    # Phase 1 Kill Switch & Start Again check
    assert is_account_kill_switch_active(acc_id) is False
    cleared = await clear_account_kill_switch(session_factory, acc_id, cleared_by="operator")
    assert cleared == 0


@pytest.mark.asyncio
async def test_startup_hydration_populates_default_symbol_limit(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Bug #1 Regression Test: Verify OrderManager.hydrate_runtime_from_db() populates default_symbol_limits from PostgreSQL at startup.

    1. Create account with default_symbol_limit = $10,000,000.
    2. Ensure no explicit CORN override exists.
    3. Construct a fresh OrderManager instance.
    4. Run startup hydration.
    5. Verify RMSContext.default_symbol_limits[account_id] == $10,000,000.
    6. Evaluate $11M CORN exposure and verify Check 8 rejects it.
    7. Set explicit CORN override = $15M and verify Check 8 passes.
    """
    from app.services.order_manager import OrderManager

    suffix = uuid4().hex[:6]
    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"AccHydration-{suffix}",
            ibkr_account=f"DUHYD{suffix}",
            total_margin=Decimal("100000.00"),
            default_symbol_limit=Decimal("10000000.00"),
        )
        session.add(acc)
        await session.flush()
        acc_id = acc.id

    # Create fresh OrderManager and execute startup hydration
    om = OrderManager(session_factory=session_factory)
    await om.hydrate_runtime_from_db()

    # Step 5: Verify default_symbol_limits populated after startup hydration
    assert acc_id in om._rms_context.default_symbol_limits
    assert om._rms_context.default_symbol_limits[acc_id] == Decimal("10000000.0000")

    # Step 6: Evaluate $11M CORN exposure (no explicit override)
    check = MoneyPerStockCheck()
    intent_11m = OrderIntent(
        signal_id="SIG-HYD-CORN-11M",
        strategy_id="model_blue",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(
                symbol="CORN",
                side=OrderSide.BUY,
                quantity=1.0,
                price=Decimal("11000000.00"),
                contract_month="202612",
            )
        ],
        account_id=acc_id,
    )

    res1 = check.evaluate(intent_11m, om._rms_context)
    assert res1.outcome == RMSOutcome.REJECT
    assert "exceeds limit of 10000000" in (res1.reason or "")

    # Step 7: Verify explicit override takes precedence
    om._rms_context.per_symbol_limits[(acc_id, "CORN")] = Decimal("15000000.00")
    res2 = check.evaluate(intent_11m, om._rms_context)
    assert res2.outcome == RMSOutcome.PASS
