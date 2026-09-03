"""Unit tests for AccountStrategyConfigService."""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.accounts.config_service import (
    AccountStrategyConfigService,
    AllocationConfigError,
)
from app.db.models.account import AccountModel, PerSymbolLimitModel
from app.db.models.strategy import StrategyModel
from app.db.session import create_engine_from_settings


@pytest.fixture
async def db_factory():
    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_account_strategy(
    session: AsyncSession,
    *,
    suffix: str | None = None,
    strategy_max_open: int = 10,
) -> tuple[AccountModel, StrategyModel]:
    account = AccountModel(
        name=f"cfg-{suffix}",
        ibkr_account=f"DUCFG{suffix}",
        total_margin=Decimal(100000),
        enabled=True,
    )
    strategy = StrategyModel(
        strategy_id=f"STRAT_{suffix}",
        legs=2,
        expression="CFD",
        max_open_positions=strategy_max_open,
        weight_source="payload",
        enabled=True,
    )
    session.add_all([account, strategy])
    await session.flush()
    return account, strategy


@pytest.mark.asyncio
async def test_create_allocation_defaults_max_open_from_strategy(db_factory) -> None:
    async with db_factory() as session:
        account, strategy = await _seed_account_strategy(session)
        svc = AccountStrategyConfigService(session)
        row = await svc.create_allocation(
            account=account,
            strategy_id=strategy.strategy_id,
            alloc_pct=Decimal("0.25"),
            target=Decimal(500),
            stop=Decimal(250),
            time_limit=3600,
        )
        await session.commit()
        assert row.max_open_positions == strategy.max_open_positions
        await session.delete(row)
        await session.delete(account)
        await session.delete(strategy)
        await session.commit()


@pytest.mark.asyncio
async def test_update_allocation_rejects_sum_over_one(db_factory) -> None:
    async with db_factory() as session:
        account, strategy = await _seed_account_strategy(session)
        svc = AccountStrategyConfigService(session)
        await svc.create_allocation(
            account=account,
            strategy_id=strategy.strategy_id,
            alloc_pct=Decimal("0.80"),
            target=Decimal(500),
            stop=Decimal(250),
            time_limit=3600,
            max_open_positions=5,
        )
        strategy2 = StrategyModel(
            strategy_id=f"STRAT_{uuid.uuid4().hex[:8]}_b",
            legs=2,
            expression="CFD",
            max_open_positions=10,
            weight_source="payload",
            enabled=True,
        )
        session.add(strategy2)
        await session.flush()
        second = await svc.create_allocation(
            account=account,
            strategy_id=strategy2.strategy_id,
            alloc_pct=Decimal("0.10"),
            target=Decimal(500),
            stop=Decimal(250),
            time_limit=3600,
            enabled=False,
        )
        await session.flush()
        with pytest.raises(AllocationConfigError, match="ALLOC_PCT_SUM_EXCEEDED"):
            await svc.update_allocation(second, alloc_pct=Decimal("0.30"), enabled=True)
        await session.rollback()


@pytest.mark.asyncio
async def test_update_account_rejects_non_positive_margin(db_factory) -> None:
    async with db_factory() as session:
        account, strategy = await _seed_account_strategy(session)
        svc = AccountStrategyConfigService(session)
        with pytest.raises(AllocationConfigError, match="INVALID_TOTAL_MARGIN"):
            await svc.update_account(account, total_margin=Decimal(0))
        await session.delete(account)
        await session.delete(strategy)
        await session.commit()


@pytest.mark.asyncio
async def test_symbol_limit_upsert_and_delete(db_factory) -> None:
    async with db_factory() as session:
        account, strategy = await _seed_account_strategy(session)
        svc = AccountStrategyConfigService(session)
        row = await svc.upsert_symbol_limit(
            account_id=account.id,
            symbol="xle",
            money_limit=Decimal(50000),
        )
        assert row.symbol == "XLE"
        row2 = await svc.upsert_symbol_limit(
            account_id=account.id,
            symbol="XLE",
            money_limit=Decimal(75000),
        )
        assert row2.money_limit == Decimal(75000)
        deleted = await svc.delete_symbol_limit(account_id=account.id, symbol="XLE")
        assert deleted is True
        remaining = (
            await session.execute(
                select(PerSymbolLimitModel).where(
                    PerSymbolLimitModel.account_id == account.id
                )
            )
        ).scalars().all()
        assert remaining == []
        await session.delete(account)
        await session.delete(strategy)
        await session.commit()


@pytest.mark.asyncio
async def test_create_allocation_defaults_pair_max_allocation_pct(db_factory) -> None:
    async with db_factory() as session:
        account, strategy = await _seed_account_strategy(session, suffix=uuid.uuid4().hex[:8])
        svc = AccountStrategyConfigService(session)
        row = await svc.create_allocation(
            account=account,
            strategy_id=strategy.strategy_id,
            alloc_pct=Decimal("0.25"),
            target=Decimal(500),
            stop=Decimal(250),
            time_limit=3600,
        )
        assert row.pair_max_allocation_pct == Decimal("0.10")
        await session.rollback()


@pytest.mark.asyncio
async def test_pair_max_allocation_pct_rejects_zero_and_over_one(db_factory) -> None:
    async with db_factory() as session:
        account, strategy = await _seed_account_strategy(session, suffix=uuid.uuid4().hex[:8])
        svc = AccountStrategyConfigService(session)
        with pytest.raises(AllocationConfigError, match="PAIR_MAX_ALLOCATION_PCT_INVALID"):
            await svc.create_allocation(
                account=account,
                strategy_id=strategy.strategy_id,
                alloc_pct=Decimal("0.25"),
                target=Decimal(500),
                stop=Decimal(250),
                time_limit=3600,
                pair_max_allocation_pct=Decimal("0"),
            )
        with pytest.raises(AllocationConfigError, match="PAIR_MAX_ALLOCATION_PCT_INVALID"):
            await svc.create_allocation(
                account=account,
                strategy_id=strategy.strategy_id,
                alloc_pct=Decimal("0.25"),
                target=Decimal(500),
                stop=Decimal(250),
                time_limit=3600,
                pair_max_allocation_pct=Decimal("1.01"),
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_pair_budget_too_small_at_one_thousand(db_factory) -> None:
    async with db_factory() as session:
        account, strategy = await _seed_account_strategy(session, suffix=uuid.uuid4().hex[:8])
        account.total_margin = Decimal("1000")
        svc = AccountStrategyConfigService(session)
        with pytest.raises(AllocationConfigError, match="PAIR_BUDGET_TOO_SMALL"):
            await svc.create_allocation(
                account=account,
                strategy_id=strategy.strategy_id,
                alloc_pct=Decimal("1"),
                target=Decimal(500),
                stop=Decimal(250),
                time_limit=3600,
                pair_max_allocation_pct=Decimal("0.10"),
            )
        await session.rollback()
