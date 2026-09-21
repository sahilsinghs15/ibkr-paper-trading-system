"""Manual-flatten reconciliation must not trust a cross-instant broker comparison.

`_reconcile_and_finalize_manual` falls back to a per-symbol quantity comparison when
the close order never reached FILLED:

    broker_qty_by_symbol[sym] == engine_qty_by_symbol[sym]  ->  "the manual lot is gone"

The left side is a snapshot of IBKR taken at `broker_positions.as_of`; the right side
is live `positions` state. Manual scope deliberately does not block engine signals, so
on a shared symbol the engine can open or close a leg between the snapshot and the
comparison — the two sides then describe different moments and the equality is
meaningless. A snapshot older than the close order is equally useless: it cannot be
evidence that the order filled.

Acting on either produced a real data-integrity error, because the branch mutates
status: a still-open lot marked FLATTENED_PENDING_PRICE tells the operator it is flat.

The fix gates the branch on evidence quality and otherwise leaves the lot unresolved
for `retry_unresolved_operations`. These tests pin both directions — the gate must not
block legitimate resolution either.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.account import AccountModel
from app.db.models.broker_position import BrokerPositionModel
from app.db.models.manual_order import (
    ManualExecutionModel,
    ManualOrderModel,
    ManualPositionModel,
)
from app.db.models.position import PositionModel
from app.services.kill_switch import KillSwitchService

SYMBOL = "AAPL"
CON_ID = 1001


async def _account(session_factory: async_sessionmaker[AsyncSession]) -> tuple[int, str]:
    tag = uuid4().hex[:6]
    async with session_factory() as s, s.begin():
        acc = AccountModel(
            name=f"KSRace-{tag}",
            ibkr_account=f"DU{tag.upper()}",
            total_margin=Decimal("100000.00"),
            enabled=True,
        )
        s.add(acc)
        await s.flush()
        return acc.id, acc.ibkr_account


async def _manual_lot(
    session_factory: async_sessionmaker[AsyncSession], account_id: int
) -> str:
    trade_id = f"MAN-RACE-{uuid4().hex[:6]}"
    async with session_factory() as s, s.begin():
        s.add(
            ManualPositionModel(
                account_id=account_id,
                trade_id=trade_id,
                symbol=SYMBOL,
                con_id=CON_ID,
                sec_type="CFD",
                currency="USD",
                signed_qty=Decimal(50),
                avg_cost=Decimal("150.00"),
                realized_pnl=Decimal(0),
                status="OPEN",
            )
        )
    return trade_id


async def _arm_and_submit(
    session_factory: async_sessionmaker[AsyncSession], account_id: int
):
    """Arm the manual flatten and leave its close order SUBMITTED, not FILLED.

    That is exactly the state where reconciliation falls through to the broker
    comparison — the branch under test.
    """
    client = MagicMock()
    client.allocate_next_order_id.return_value = 7001
    client.placeOrder = MagicMock()
    svc = KillSwitchService(session_factory=session_factory, client=client)
    op, _ = await svc.initiate_manual_square_off(account_id)
    await svc._execute_manual_flatten_operation(op.operation_id)
    return svc, op


async def _close_order(
    session_factory: async_sessionmaker[AsyncSession], account_id: int
) -> ManualOrderModel:
    async with session_factory() as s:
        return (
            await s.execute(
                select(ManualOrderModel).where(
                    ManualOrderModel.account_id == account_id,
                    ManualOrderModel.source == "ks_manual",
                )
            )
        ).scalar_one()


async def _set_broker_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    ibkr_account: str,
    account_id: int,
    signed_qty: Decimal,
    as_of: datetime,
) -> None:
    async with session_factory() as s, s.begin():
        s.add(
            BrokerPositionModel(
                ibkr_account=ibkr_account,
                con_id=CON_ID,
                account_id=account_id,
                symbol=SYMBOL,
                sec_type="CFD",
                currency="USD",
                exchange="SMART",
                signed_qty=signed_qty,
                avg_cost=Decimal("150.00"),
                as_of=as_of,
            )
        )


async def _lot(
    session_factory: async_sessionmaker[AsyncSession], trade_id: str
) -> ManualPositionModel:
    async with session_factory() as s:
        return (
            await s.execute(
                select(ManualPositionModel).where(
                    ManualPositionModel.trade_id == trade_id
                )
            )
        ).scalar_one()


# ---------------------------------------------------------------------------
# The gate must not break legitimate resolution
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fresh_snapshot_with_quiet_engine_still_resolves(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Snapshot post-dates the close order and no engine leg moved -> trusted."""
    account_id, ibkr = await _account(session_factory)
    trade_id = await _manual_lot(session_factory, account_id)
    svc, op = await _arm_and_submit(session_factory, account_id)
    order = await _close_order(session_factory, account_id)

    # Broker reports flat for the symbol, observed after the order went out.
    await _set_broker_snapshot(
        session_factory,
        ibkr_account=ibkr,
        account_id=account_id,
        signed_qty=Decimal(0),
        as_of=(order.submitted_at or datetime.now(UTC)) + timedelta(seconds=5),
    )
    async with session_factory() as s, s.begin():
        s.add(
            ManualExecutionModel(
                exec_id=f"exec-{uuid4().hex[:6]}",
                manual_order_id=order.id,
                quantity=Decimal(50),
                price=Decimal("155.00"),
                executed_at=datetime.now(UTC),
            )
        )

    await svc._reconcile_and_finalize_manual(op.operation_id, account_id)

    lot = await _lot(session_factory, trade_id)
    assert lot.status == "CLOSED"
    assert lot.signed_qty == Decimal(0)
    assert lot.realized_pnl == Decimal("250.00")  # 50 * (155 - 150)


@pytest.mark.asyncio
async def test_fresh_snapshot_without_executions_marks_pending_price(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Trusted broker evidence but no fill price -> PENDING_PRICE, never fabricated."""
    account_id, ibkr = await _account(session_factory)
    trade_id = await _manual_lot(session_factory, account_id)
    svc, op = await _arm_and_submit(session_factory, account_id)
    order = await _close_order(session_factory, account_id)

    await _set_broker_snapshot(
        session_factory,
        ibkr_account=ibkr,
        account_id=account_id,
        signed_qty=Decimal(0),
        as_of=(order.submitted_at or datetime.now(UTC)) + timedelta(seconds=5),
    )

    await svc._reconcile_and_finalize_manual(op.operation_id, account_id)

    lot = await _lot(session_factory, trade_id)
    assert lot.status == "FLATTENED_PENDING_PRICE"
    assert lot.realized_pnl == Decimal(0)


# ---------------------------------------------------------------------------
# The race itself
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_engine_leg_opened_after_snapshot_is_not_treated_as_evidence(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An engine signal on the same symbol invalidates the cross-instant compare.

    Broker snapshot says 0 for AAPL. Afterwards an engine pair opens +50 AAPL, so
    live engine qty is 50 and the manual lot is still open at the broker. The naive
    comparison (broker 0 vs engine... ) must not be allowed to resolve the lot.
    """
    account_id, ibkr = await _account(session_factory)
    trade_id = await _manual_lot(session_factory, account_id)
    svc, op = await _arm_and_submit(session_factory, account_id)
    order = await _close_order(session_factory, account_id)

    snapshot_at = (order.submitted_at or datetime.now(UTC)) + timedelta(seconds=5)
    await _set_broker_snapshot(
        session_factory,
        ibkr_account=ibkr,
        account_id=account_id,
        signed_qty=Decimal(0),
        as_of=snapshot_at,
    )

    # Engine opens a leg on the same symbol AFTER the broker was observed.
    async with session_factory() as s, s.begin():
        s.add(
            PositionModel(
                account_id=account_id,
                trade_id=f"ENG-{uuid4().hex[:6]}",
                strategy_id="model_blue",
                leg_a_symbol=SYMBOL,
                leg_a_signed_qty=Decimal(0),
                leg_a_entry_mark=Decimal("150.00"),
                target=Decimal("0.05"),
                stop=Decimal("0.02"),
                time_limit=60,
                risk_state="OPEN",
                opened_at=snapshot_at + timedelta(seconds=1),
            )
        )

    await svc._reconcile_and_finalize_manual(op.operation_id, account_id)

    lot = await _lot(session_factory, trade_id)
    assert lot.status == "OPEN", (
        "a lot must never be resolved from a broker/engine comparison spanning "
        "two different moments"
    )
    assert lot.signed_qty == Decimal(50)
    assert lot.closed_at is None


@pytest.mark.asyncio
async def test_snapshot_older_than_close_order_is_not_evidence(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A snapshot taken before the close order was sent cannot prove it filled."""
    account_id, ibkr = await _account(session_factory)
    trade_id = await _manual_lot(session_factory, account_id)
    svc, op = await _arm_and_submit(session_factory, account_id)
    order = await _close_order(session_factory, account_id)

    await _set_broker_snapshot(
        session_factory,
        ibkr_account=ibkr,
        account_id=account_id,
        signed_qty=Decimal(0),
        as_of=(order.submitted_at or datetime.now(UTC)) - timedelta(minutes=10),
    )

    await svc._reconcile_and_finalize_manual(op.operation_id, account_id)

    lot = await _lot(session_factory, trade_id)
    assert lot.status == "OPEN"
    assert lot.signed_qty == Decimal(50)


@pytest.mark.asyncio
async def test_unresolved_lot_converges_once_the_engine_settles(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Holding back is safe because a later snapshot resolves it.

    Guards against the gate turning a transient race into a permanent stall.
    """
    account_id, ibkr = await _account(session_factory)
    trade_id = await _manual_lot(session_factory, account_id)
    svc, op = await _arm_and_submit(session_factory, account_id)
    order = await _close_order(session_factory, account_id)
    submitted = order.submitted_at or datetime.now(UTC)

    stale_at = submitted + timedelta(seconds=5)
    await _set_broker_snapshot(
        session_factory,
        ibkr_account=ibkr,
        account_id=account_id,
        signed_qty=Decimal(0),
        as_of=stale_at,
    )
    async with session_factory() as s, s.begin():
        s.add(
            PositionModel(
                account_id=account_id,
                trade_id=f"ENG-{uuid4().hex[:6]}",
                strategy_id="model_blue",
                leg_a_symbol=SYMBOL,
                leg_a_signed_qty=Decimal(0),
                leg_a_entry_mark=Decimal("150.00"),
                target=Decimal("0.05"),
                stop=Decimal("0.02"),
                time_limit=60,
                risk_state="OPEN",
                opened_at=stale_at + timedelta(seconds=1),
            )
        )

    await svc._reconcile_and_finalize_manual(op.operation_id, account_id)
    assert (await _lot(session_factory, trade_id)).status == "OPEN"

    # Next sweep: a newer snapshot now post-dates the engine open.
    async with session_factory() as s, s.begin():
        row = (
            await s.execute(
                select(BrokerPositionModel).where(
                    BrokerPositionModel.account_id == account_id
                )
            )
        ).scalar_one()
        row.as_of = stale_at + timedelta(seconds=30)

    await svc._reconcile_and_finalize_manual(op.operation_id, account_id)

    lot = await _lot(session_factory, trade_id)
    assert lot.status == "FLATTENED_PENDING_PRICE", (
        "the gate must delay resolution, not prevent it"
    )
