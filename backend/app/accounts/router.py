"""Resolve strategy_id -> enabled Account × Strategy execution contexts.

Does not size, evaluate RMS, submit OMS, or call IBKR.
"""

from collections.abc import Sequence
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.accounts.context import AccountExecutionContext
from app.db.models.account import AccountModel
from app.db.models.strategy import AllocationModel, StrategyModel

SessionFactory = async_sessionmaker[AsyncSession]


class StrategyAccountRouter(Protocol):
    """Signal strategy -> eligible account execution contexts."""

    async def resolve(self, strategy_id: str) -> list[AccountExecutionContext]:
        """Return enabled subscriptions for ``strategy_id``. Never infers a default account."""


class StaticStrategyAccountRouter:
    """Test/harness router with an explicit context list. No implicit first-account pick."""

    def __init__(self, contexts: Sequence[AccountExecutionContext]) -> None:
        self._contexts = list(contexts)

    async def resolve(self, strategy_id: str) -> list[AccountExecutionContext]:
        wanted = (strategy_id or "").strip()
        return [ctx for ctx in self._contexts if ctx.strategy_id == wanted]


class DatabaseStrategyAccountRouter:
    """Load Account × Strategy rows that are fully enabled for the incoming strategy."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def resolve(self, strategy_id: str) -> list[AccountExecutionContext]:
        wanted = (strategy_id or "").strip()
        if not wanted:
            return []
        async with self._session_factory() as session:
            stmt = (
                select(AccountModel, AllocationModel, StrategyModel)
                .join(AllocationModel, AllocationModel.account_id == AccountModel.id)
                .join(
                    StrategyModel,
                    StrategyModel.strategy_id == AllocationModel.strategy_id,
                )
                .where(
                    AllocationModel.strategy_id == wanted,
                    AccountModel.enabled.is_(True),
                    StrategyModel.enabled.is_(True),
                    AllocationModel.enabled.is_(True),
                    AccountModel.total_margin > 0,
                    AllocationModel.alloc_pct > 0,
                )
                .order_by(AccountModel.id)
            )
            rows = (await session.execute(stmt)).all()
        contexts: list[AccountExecutionContext] = []
        for account, allocation, strategy in rows:
            committed = account.total_margin * allocation.alloc_pct
            if committed <= 0:
                continue
            contexts.append(
                AccountExecutionContext(
                    account_id=account.id,
                    ibkr_account=account.ibkr_account,
                    strategy_id=strategy.strategy_id,
                    total_margin=account.total_margin,
                    alloc_pct=allocation.alloc_pct,
                    committed_notional=committed,
                    target=allocation.target,
                    stop=allocation.stop,
                    time_limit=allocation.time_limit,
                    max_open_positions=strategy.max_open_positions,
                )
            )
        return contexts


def context_from_rows(
    account: AccountModel,
    allocation: AllocationModel,
    strategy: StrategyModel,
) -> AccountExecutionContext:
    """Build a context from already-loaded ORM rows (tests / config service)."""
    committed = account.total_margin * allocation.alloc_pct
    return AccountExecutionContext(
        account_id=account.id,
        ibkr_account=account.ibkr_account,
        strategy_id=strategy.strategy_id,
        total_margin=account.total_margin,
        alloc_pct=allocation.alloc_pct,
        committed_notional=Decimal(committed),
        target=allocation.target,
        stop=allocation.stop,
        time_limit=allocation.time_limit,
        max_open_positions=strategy.max_open_positions,
    )
