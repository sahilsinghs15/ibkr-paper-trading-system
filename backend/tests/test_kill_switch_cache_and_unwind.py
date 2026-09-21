"""Regression tests for two kill-switch defects.

(a) Manual-scope hot cache released only on COMPLETE
    ``_reconcile_and_finalize_manual`` discarded the account from
    ``_MANUAL_KILL_SWITCH_ACTIVE_ACCOUNTS`` only when ``unresolved == 0``. An
    UNRESOLVED manual flatten therefore blocked manual order submission in-process
    forever, while ``hydrate_kill_switch_cache`` (which rebuilds from
    ``_MANUAL_FLATTEN_ACTIVE_STATUSES``, excluding UNRESOLVED) silently unblocked it
    after a restart. That also locked the operator out of the manual closes needed to
    resolve the operation. Engine scope is deliberately the opposite: UNRESOLVED stays
    armed until an explicit operator clear.

(b) Compensation suppressed while the kill switch is armed
    ``BasketCoordinator._compensate_filled`` returned ``[]`` for OPEN intents whenever
    the account kill switch was active. Compensation emits reverse CLOSE legs that only
    reduce exposure, so skipping it stranded the filled leg naked -- an unsettled basket
    writes no ``positions`` row, so the kill-switch flatten snapshot cannot see it -- and
    forced the basket to CRITICAL instead of unwinding to COMPENSATED. The remainder-retry
    guard must stay, because a retry ADDS exposure.
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.account import AccountModel
from app.db.models.kill_switch import (
    KILL_SWITCH_STATUS_UNRESOLVED,
    KillSwitchOperationModel,
)
from app.db.models.manual_order import ManualPositionModel
from app.oms.basket import Basket, BasketState
from app.oms.coordinator import BasketCoordinator
from app.oms.models import ExecutionResult, OMSOrder, OMSOrderStatus
from app.oms.oms_service import OMSService
from app.rms.models import (
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
    RMSOutcome,
    RMSResult,
)
from app.schemas.manual_schemas import ManualOrderSubmitRequest
from app.services.kill_switch import (
    KillSwitchService,
    hydrate_kill_switch_cache,
    is_account_kill_switch_active,
    is_manual_kill_switch_active,
)
from app.services.manual_trading import ManualTradingService


async def _create_account(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[int, str]:
    tag = uuid4().hex[:6]
    async with session_factory() as s, s.begin():
        acc = AccountModel(
            name=f"KSCache-{tag}",
            ibkr_account=f"DU{tag.upper()}",
            total_margin=Decimal("100000.00"),
            enabled=True,
        )
        s.add(acc)
        await s.flush()
        return acc.id, acc.ibkr_account


async def _open_manual_position(
    session_factory: async_sessionmaker[AsyncSession], account_id: int
) -> str:
    trade_id = f"MAN-UNRES-{uuid4().hex[:6]}"
    async with session_factory() as s, s.begin():
        s.add(
            ManualPositionModel(
                account_id=account_id,
                trade_id=trade_id,
                symbol="AAPL",
                con_id=1001,
                sec_type="CFD",
                currency="USD",
                signed_qty=Decimal(50),
                avg_cost=Decimal("150.00"),
                realized_pnl=Decimal(0),
                status="OPEN",
            )
        )
    return trade_id


async def _drive_manual_flatten_to_unresolved(
    session_factory: async_sessionmaker[AsyncSession], account_id: int
) -> KillSwitchService:
    """Snapshot an open manual position, then reconcile without any close fills.

    With no ``ks_manual`` close orders and no broker rows, every snapshotted position
    stays unresolved, so the operation finalises UNRESOLVED.
    """
    client = MagicMock()
    client.is_connected.return_value = True
    svc = KillSwitchService(session_factory=session_factory, client=client)
    op, created = await svc.initiate_manual_square_off(account_id)
    assert created
    assert is_manual_kill_switch_active(account_id), "cache must arm on initiation"

    await svc._reconcile_and_finalize_manual(op.operation_id, account_id)

    async with session_factory() as s:
        row = (
            await s.execute(
                select(KillSwitchOperationModel).where(
                    KillSwitchOperationModel.operation_id == op.operation_id
                )
            )
        ).scalar_one()
        assert row.status == KILL_SWITCH_STATUS_UNRESOLVED
    return svc


# ---------------------------------------------------------------------------
# (a) manual-scope cache release
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unresolved_manual_flatten_releases_manual_block(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """UNRESOLVED is terminal for manual scope: the hot cache must release."""
    account_id, _ = await _create_account(session_factory)
    await _open_manual_position(session_factory, account_id)

    await _drive_manual_flatten_to_unresolved(session_factory, account_id)

    assert not is_manual_kill_switch_active(account_id)


@pytest.mark.asyncio
async def test_unresolved_manual_cache_survives_restart_identically(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """In-process cache must equal what a restart rebuilds from PostgreSQL."""
    account_id, _ = await _create_account(session_factory)
    await _open_manual_position(session_factory, account_id)

    await _drive_manual_flatten_to_unresolved(session_factory, account_id)
    in_process = is_manual_kill_switch_active(account_id)

    # Simulate a process restart: cache is rebuilt purely from the DB.
    await hydrate_kill_switch_cache(session_factory)
    after_restart = is_manual_kill_switch_active(account_id)

    assert in_process == after_restart is False


@pytest.mark.asyncio
async def test_unresolved_manual_flatten_allows_operator_to_close_manually(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The operator must be able to submit the manual closes that resolve the op."""
    account_id, _ = await _create_account(session_factory)
    await _open_manual_position(session_factory, account_id)

    await _drive_manual_flatten_to_unresolved(session_factory, account_id)

    client = MagicMock()
    client.is_connected.return_value = True
    async with session_factory() as session:
        account = await session.get(AccountModel, account_id)
        assert account is not None
        manual_svc = ManualTradingService(session=session, client=client)
        req = ManualOrderSubmitRequest(
            con_id=1001,
            symbol="AAPL",
            sec_type="CFD",
            exchange="SMART",
            currency="USD",
            side="SELL",
            quantity=Decimal(50),
            order_type="MARKET",
            tif="DAY",
            idempotency_key=f"idem-{uuid4().hex[:6]}",
        )
        valid, errors, _ = await manual_svc.validate_pretrade(account, req)

    assert valid, errors
    assert not any("kill switch" in err.lower() for err in errors)


@pytest.mark.asyncio
async def test_engine_scope_unresolved_stays_armed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Engine scope must NOT adopt the manual release semantics."""
    account_id, _ = await _create_account(session_factory)

    svc = KillSwitchService(session_factory=session_factory)
    op, created = await svc.initiate_square_off(account_id)
    assert created
    assert is_account_kill_switch_active(account_id)

    await svc._update_operation_completion(
        op.operation_id, final_status=KILL_SWITCH_STATUS_UNRESOLVED, unresolved=1
    )

    assert is_account_kill_switch_active(account_id), (
        "engine kill switch must stay armed through UNRESOLVED until operator clear"
    )
    await hydrate_kill_switch_cache(session_factory)
    assert is_account_kill_switch_active(account_id)


# ---------------------------------------------------------------------------
# (b) compensation must not be suppressed by an armed kill switch
# ---------------------------------------------------------------------------
def _open_intent(account_id: int = 4242) -> OrderIntent:
    return OrderIntent(
        signal_id="T-UNWIND-KS",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        account_id=account_id,
        ibkr_account="DU99999",
        legs=[
            OrderLeg(
                symbol="EWP",
                side=OrderSide.BUY,
                quantity=Decimal(100),  # pyrefly: ignore[bad-argument-type]
                price=Decimal(50),
                instrument_type="STK",
                leg_index=0,
            ),
            OrderLeg(
                symbol="EWU",
                side=OrderSide.SELL,
                quantity=Decimal(200),  # pyrefly: ignore[bad-argument-type]
                price=Decimal(30),
                instrument_type="STK",
                leg_index=1,
            ),
        ],
        timestamp=datetime.now(UTC),
    )


def _coordinator_with_submit_stub() -> tuple[BasketCoordinator, MagicMock]:
    oms = MagicMock(spec=OMSService)

    async def _submit(comp_intent: OrderIntent, rms: RMSResult, **_kw) -> ExecutionResult:
        leg = comp_intent.legs[0]
        child = OMSOrder(
            internal_order_id=f"ORD-COMP-{leg.symbol}",
            intent=comp_intent,
            symbol=leg.symbol,
            side=leg.side,
            quantity=float(leg.quantity),
            filled_quantity=float(leg.quantity),
            status=OMSOrderStatus.FILLED,
            leg_index=leg.leg_index,
        )
        return ExecutionResult(
            order=child,
            rms_result=RMSResult(
                outcome=RMSOutcome.PASS,
                intent=comp_intent,
                original_intent=comp_intent,
            ),
            success=True,
            orders=[child],
        )

    oms.submit_intent = AsyncMock(side_effect=_submit)
    return BasketCoordinator(oms), oms


@pytest.mark.asyncio
async def test_compensation_runs_while_kill_switch_armed() -> None:
    """A filled leg must still be unwound when the kill switch is armed."""
    from app.services.kill_switch import _KILL_SWITCH_ACTIVE_ACCOUNTS

    intent = _open_intent()
    assert intent.account_id is not None
    _KILL_SWITCH_ACTIVE_ACCOUNTS.add(intent.account_id)

    # Leg 0 filled, leg 1 never filled -> naked EWP exposure needing an unwind.
    submitted = [
        OMSOrder(
            internal_order_id="ORD-EWP-1",
            intent=intent,
            symbol="EWP",
            side=OrderSide.BUY,
            quantity=100.0,
            filled_quantity=100.0,
            status=OMSOrderStatus.FILLED,
            leg_index=0,
        ),
        OMSOrder(
            internal_order_id="ORD-EWU-1",
            intent=intent,
            symbol="EWU",
            side=OrderSide.SELL,
            quantity=200.0,
            filled_quantity=0.0,
            status=OMSOrderStatus.CANCELLED,
            leg_index=1,
        ),
    ]
    basket = Basket(
        account_id=intent.account_id,
        trade_id=intent.signal_id,
        strategy_id=intent.strategy_id,
        action="OPEN",
        intended_leg_count=2,
        state=BasketState.UNWINDING,
    )

    coord, oms = _coordinator_with_submit_stub()
    compensation = await coord._compensate_filled(
        intent, submitted, order_type="MARKET", signal_pk=None, basket=basket
    )

    assert len(compensation) == 1, "filled leg must be unwound despite armed kill switch"
    comp = compensation[0]
    assert comp.symbol == "EWP"
    assert comp.side == OrderSide.SELL, "unwind must reverse the original BUY"
    assert comp.is_compensation is True
    assert comp.quantity == 100.0

    submitted_intent = oms.submit_intent.await_args[0][0]
    assert submitted_intent.action == OrderAction.CLOSE

    # The basket can now reach COMPENSATED instead of being forced CRITICAL.
    assert coord._compensation_complete(compensation, submitted=submitted) is True


@pytest.mark.asyncio
async def test_remainder_retry_still_blocked_by_kill_switch() -> None:
    """The remainder-retry guard must remain: a retry ADDS exposure."""
    from app.services.kill_switch import _KILL_SWITCH_ACTIVE_ACCOUNTS

    intent = _open_intent()
    assert intent.account_id is not None
    _KILL_SWITCH_ACTIVE_ACCOUNTS.add(intent.account_id)

    coord, oms = _coordinator_with_submit_stub()
    oms.cancel_order = AsyncMock()
    coord._retries_enabled = MagicMock(return_value=True)  # type: ignore[method-assign]

    basket = Basket(
        account_id=intent.account_id,
        trade_id=intent.signal_id,
        strategy_id=intent.strategy_id,
        action="OPEN",
        intended_leg_count=2,
        state=BasketState.EXECUTING,
    )
    submitted = [
        OMSOrder(
            internal_order_id="ORD-EWP-1",
            intent=intent,
            symbol="EWP",
            side=OrderSide.BUY,
            quantity=100.0,
            filled_quantity=40.0,
            status=OMSOrderStatus.SUBMITTED,
            leg_index=0,
        )
    ]

    retried = await coord._retry_incomplete(
        intent, submitted, order_type="MARKET", signal_pk=None, basket=basket
    )

    assert retried == []
    coord._retries_enabled.assert_not_called()
    oms.cancel_order.assert_not_awaited()
