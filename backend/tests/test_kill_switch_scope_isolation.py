"""Kill-switch scope isolation: each scope blocks only the ledger it owns.

    ENGINE  ("Flatten Signal Positions") -> blocks engine OPENs only.
                                            Manual trading stays open, because the
                                            engine flatten deliberately preserves
                                            manual positions -- blocking the ticket
                                            would strand the operator with manual
                                            exposure they cannot close.
    MANUAL  ("Flatten Manual Positions") -> blocks manual orders only.
                                            Engine signals keep trading.
    ACCOUNT ("Complete Flatten")         -> blocks both ledgers.

Previously ENGINE and ACCOUNT armed the same ``_KILL_SWITCH_ACTIVE_ACCOUNTS`` set, so
the manual ticket could not tell them apart and an engine-only flatten blocked manual
trading outright.
"""

from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.account import AccountModel
from app.db.models.kill_switch import (
    KILL_SWITCH_SCOPE_ACCOUNT,
    KILL_SWITCH_SCOPE_ENGINE,
    KILL_SWITCH_SCOPE_MANUAL,
)
from app.db.models.manual_order import ManualPositionModel
from app.schemas.manual_schemas import ManualOrderSubmitRequest
from app.services.kill_switch import (
    KillSwitchService,
    clear_account_kill_switch,
    hydrate_kill_switch_cache,
    is_account_kill_switch_active,
    is_account_scope_kill_switch_active,
    is_manual_kill_switch_active,
    is_manual_trading_blocked,
)
from app.services.manual_trading import ManualTradingService
from app.services.risk_exit_monitor import RiskExitMonitor
from app.services.risk_exit_rules import REASON_ACCOUNT_STOP, ExitDecision


async def _create_account(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[int, str]:
    tag = uuid4().hex[:6]
    async with session_factory() as s, s.begin():
        acc = AccountModel(
            name=f"KSScope-{tag}",
            ibkr_account=f"DU{tag.upper()}",
            total_margin=Decimal("100000.00"),
            enabled=True,
        )
        s.add(acc)
        await s.flush()
        return acc.id, acc.ibkr_account


async def _open_manual_position(
    session_factory: async_sessionmaker[AsyncSession], account_id: int
) -> None:
    async with session_factory() as s, s.begin():
        s.add(
            ManualPositionModel(
                account_id=account_id,
                trade_id=f"MAN-SCOPE-{uuid4().hex[:6]}",
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


def _manual_request(side: str = "SELL") -> ManualOrderSubmitRequest:
    return ManualOrderSubmitRequest(
        con_id=1001,
        symbol="AAPL",
        sec_type="CFD",
        exchange="SMART",
        currency="USD",
        side=side,
        quantity=Decimal(10),
        order_type="MARKET",
        tif="DAY",
        idempotency_key=f"idem-{uuid4().hex[:6]}",
    )


async def _validate_manual(
    session_factory: async_sessionmaker[AsyncSession], account_id: int
) -> tuple[bool, list[str]]:
    client = MagicMock()
    client.is_connected.return_value = True
    async with session_factory() as session:
        account = await session.get(AccountModel, account_id)
        assert account is not None
        svc = ManualTradingService(session=session, client=client)
        valid, errors, _ = await svc.validate_pretrade(account, _manual_request())
    return valid, errors


# ---------------------------------------------------------------------------
# ENGINE scope -> engine only
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_engine_scope_blocks_engine_but_not_manual(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id, _ = await _create_account(session_factory)
    await _open_manual_position(session_factory, account_id)

    svc = KillSwitchService(session_factory=session_factory)
    op, created = await svc.initiate_square_off(account_id)
    assert created
    assert op.scope == KILL_SWITCH_SCOPE_ENGINE

    # Engine ledger blocked.
    assert is_account_kill_switch_active(account_id)
    # Manual ledger untouched.
    assert not is_account_scope_kill_switch_active(account_id)
    assert not is_manual_trading_blocked(account_id)

    valid, errors = await _validate_manual(session_factory, account_id)
    assert valid, errors
    assert not any("kill switch" in err.lower() for err in errors)


@pytest.mark.asyncio
async def test_engine_scope_isolation_survives_restart(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id, _ = await _create_account(session_factory)
    svc = KillSwitchService(session_factory=session_factory)
    await svc.initiate_square_off(account_id)

    await hydrate_kill_switch_cache(session_factory)

    assert is_account_kill_switch_active(account_id)
    assert not is_manual_trading_blocked(account_id)


# ---------------------------------------------------------------------------
# MANUAL scope -> manual only
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_manual_scope_blocks_manual_but_not_engine(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id, _ = await _create_account(session_factory)
    await _open_manual_position(session_factory, account_id)

    client = MagicMock()
    client.is_connected.return_value = True
    svc = KillSwitchService(session_factory=session_factory, client=client)
    op, created = await svc.initiate_manual_square_off(account_id)
    assert created
    assert op.scope == KILL_SWITCH_SCOPE_MANUAL

    # Manual ledger blocked, engine ledger free.
    assert is_manual_kill_switch_active(account_id)
    assert is_manual_trading_blocked(account_id)
    assert not is_account_kill_switch_active(account_id)
    assert not is_account_scope_kill_switch_active(account_id)

    valid, errors = await _validate_manual(session_factory, account_id)
    assert not valid
    assert any("manual kill switch is active" in err for err in errors)


# ---------------------------------------------------------------------------
# ACCOUNT scope -> both
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_account_scope_blocks_both_ledgers(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id, _ = await _create_account(session_factory)
    await _open_manual_position(session_factory, account_id)

    svc = KillSwitchService(session_factory=session_factory)
    op, created = await svc.arm_account_kill_switch_only(account_id)
    assert created
    assert op.scope == KILL_SWITCH_SCOPE_ACCOUNT

    assert is_account_kill_switch_active(account_id)
    assert is_account_scope_kill_switch_active(account_id)
    assert is_manual_trading_blocked(account_id)

    valid, errors = await _validate_manual(session_factory, account_id)
    assert not valid
    assert any("account-wide kill switch is active" in err for err in errors)


@pytest.mark.asyncio
async def test_account_scope_blocks_both_after_restart(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id, _ = await _create_account(session_factory)
    svc = KillSwitchService(session_factory=session_factory)
    await svc.arm_account_kill_switch_only(account_id)

    await hydrate_kill_switch_cache(session_factory)

    assert is_account_kill_switch_active(account_id)
    assert is_manual_trading_blocked(account_id)


@pytest.mark.asyncio
async def test_account_scope_idempotent_rearm_keeps_manual_blocked(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The early-return path for an existing ACCOUNT op must arm both caches."""
    from app.services.kill_switch import _ACCOUNT_SCOPE_KILL_SWITCH_ACCOUNTS

    account_id, _ = await _create_account(session_factory)
    svc = KillSwitchService(session_factory=session_factory)
    await svc.arm_account_kill_switch_only(account_id)

    # Simulate a cold hot-cache (e.g. a second API process) and re-arm.
    _ACCOUNT_SCOPE_KILL_SWITCH_ACCOUNTS.discard(account_id)
    _op, created = await svc.arm_account_kill_switch_only(account_id)

    assert created is False
    assert is_manual_trading_blocked(account_id)


# ---------------------------------------------------------------------------
# Clearing is per scope
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_clearing_account_scope_releases_manual_block(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    account_id, _ = await _create_account(session_factory)
    svc = KillSwitchService(session_factory=session_factory)
    await svc.arm_account_kill_switch_only(account_id)
    assert is_manual_trading_blocked(account_id)

    await clear_account_kill_switch(
        session_factory, account_id, scope=KILL_SWITCH_SCOPE_ACCOUNT
    )

    assert not is_account_scope_kill_switch_active(account_id)
    assert not is_manual_trading_blocked(account_id)
    valid, errors = await _validate_manual(session_factory, account_id)
    assert valid, errors


@pytest.mark.asyncio
async def test_engine_and_manual_scopes_compose_independently(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Engine armed + manual flatten in flight: each blocks only its own ledger."""
    account_id, _ = await _create_account(session_factory)
    await _open_manual_position(session_factory, account_id)

    client = MagicMock()
    client.is_connected.return_value = True
    svc = KillSwitchService(session_factory=session_factory, client=client)
    await svc.initiate_square_off(account_id)
    await svc.initiate_manual_square_off(account_id)

    assert is_account_kill_switch_active(account_id)
    assert is_manual_trading_blocked(account_id)
    # Neither is ACCOUNT scope, so clearing the manual one frees the manual ledger
    # while the engine ledger stays armed.
    await clear_account_kill_switch(
        session_factory, account_id, scope=KILL_SWITCH_SCOPE_MANUAL
    )
    assert is_account_kill_switch_active(account_id)
    assert not is_manual_trading_blocked(account_id)


# ---------------------------------------------------------------------------
# Automatic daily-risk breach must halt both ledgers
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_daily_risk_breach_halts_manual_trading_too(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A daily target/stop breach is account-level, so it must block manual too.

    Otherwise an account can blow its daily loss limit, have its engine book
    auto-flattened, and the operator can still open fresh manual risk on it.
    """
    account_id, _ = await _create_account(session_factory)
    await _open_manual_position(session_factory, account_id)

    ks = KillSwitchService(session_factory=session_factory)
    monitor = RiskExitMonitor(session_factory, kill_switch=ks, enabled=True)

    await monitor._handle_account_breach(
        account_id=account_id,
        decision=ExitDecision(
            reason=REASON_ACCOUNT_STOP,
            threshold=Decimal(-1000),
            pnl=Decimal(-1500),
        ),
        session_date="2026-09-21",
    )

    # Drain the background engine flatten so the test leaves no pending task.
    for task in list(ks._in_flight.values()):
        await task

    assert is_account_scope_kill_switch_active(account_id)
    assert is_account_kill_switch_active(account_id), "engine ledger must be halted"
    assert is_manual_trading_blocked(account_id), "manual ledger must be halted"

    valid, errors = await _validate_manual(session_factory, account_id)
    assert not valid
    assert any("account-wide kill switch is active" in err for err in errors)


@pytest.mark.asyncio
async def test_daily_risk_breach_does_not_auto_flatten_manual_lots(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Blocking manual is not the same as flattening it.

    Submitting broker orders against operator-owned lots stays an explicit
    operator action (Complete Flatten), per AGENTS.md section 7.
    """
    from sqlalchemy import select

    from app.db.models.manual_order import ManualOrderModel

    account_id, _ = await _create_account(session_factory)
    await _open_manual_position(session_factory, account_id)

    ks = KillSwitchService(session_factory=session_factory)
    monitor = RiskExitMonitor(session_factory, kill_switch=ks, enabled=True)
    await monitor._handle_account_breach(
        account_id=account_id,
        decision=ExitDecision(
            reason=REASON_ACCOUNT_STOP, threshold=Decimal(-1000), pnl=Decimal(-1500)
        ),
        session_date="2026-09-21",
    )
    for task in list(ks._in_flight.values()):
        await task

    async with session_factory() as session:
        manual_orders = (
            await session.execute(
                select(ManualOrderModel).where(ManualOrderModel.account_id == account_id)
            )
        ).scalars().all()
        open_lots = (
            await session.execute(
                select(ManualPositionModel).where(
                    ManualPositionModel.account_id == account_id,
                    ManualPositionModel.status == "OPEN",
                )
            )
        ).scalars().all()

    assert manual_orders == [], "no broker orders may be placed against manual lots"
    assert len(open_lots) == 1, "manual lot stays open, blocked but not flattened"
