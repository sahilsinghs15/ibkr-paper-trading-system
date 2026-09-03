"""Resolve strategy_id -> enabled Account × Strategy execution contexts.

Does not size, evaluate RMS, submit OMS, or call IBKR.
"""

import logging
from collections.abc import Sequence
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.accounts.context import AccountExecutionContext
from app.db.models.account import AccountModel
from app.db.models.strategy import AllocationModel, StrategyModel

logger = logging.getLogger(__name__)

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
        contexts = [ctx for ctx in self._contexts if ctx.strategy_id == wanted]
        logger.info(
            "Account router (static) resolved strategy_id=%s accounts=%d preview=%s",
            wanted,
            len(contexts),
            [
                {"account_id": c.account_id, "ibkr_account": c.ibkr_account}
                for c in contexts[:5]
            ],
        )
        return contexts


class DatabaseStrategyAccountRouter:
    """Load Account × Strategy rows that are fully enabled for the incoming strategy."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def resolve(
        self, strategy_id: str, *, session: AsyncSession | None = None
    ) -> list[AccountExecutionContext]:
        wanted = (strategy_id or "").strip()
        if not wanted:
            return []
        if session is not None:
            return await self._resolve(session, wanted)
        async with self._session_factory() as owned:
            return await self._resolve(owned, wanted)

    async def _resolve(
        self, session: AsyncSession, wanted: str
    ) -> list[AccountExecutionContext]:
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
            pair_budget = committed * allocation.pair_max_allocation_pct
            if pair_budget <= 0:
                logger.warning(
                    "Skipping account_id=%s strategy_id=%s: pair budget resolves to %s",
                    account.id,
                    strategy.strategy_id,
                    pair_budget,
                )
                continue
            contexts.append(
                AccountExecutionContext(
                    account_id=account.id,
                    ibkr_account=account.ibkr_account,
                    strategy_id=strategy.strategy_id,
                    total_margin=account.total_margin,
                    alloc_pct=allocation.alloc_pct,
                    committed_notional=committed,
                    pair_max_allocation_pct=allocation.pair_max_allocation_pct,
                    pair_budget=pair_budget,
                    target=allocation.target,
                    stop=allocation.stop,
                    time_limit=allocation.time_limit,
                    max_open_positions=allocation.max_open_positions,
                )
            )
        logger.info(
            "Account router resolved strategy_id=%s accounts=%d preview=%s%s",
            wanted,
            len(contexts),
            [
                {"account_id": c.account_id, "ibkr_account": c.ibkr_account}
                for c in contexts[:5]
            ],
            "" if len(contexts) <= 5 else f" (+{len(contexts) - 5} more)",
        )
        return contexts


def context_from_rows(
    account: AccountModel,
    allocation: AllocationModel,
    strategy: StrategyModel,
) -> AccountExecutionContext:
    """Build a context from already-loaded ORM rows (tests / config service)."""
    committed = account.total_margin * allocation.alloc_pct
    pair_pct = allocation.pair_max_allocation_pct
    return AccountExecutionContext(
        account_id=account.id,
        ibkr_account=account.ibkr_account,
        strategy_id=strategy.strategy_id,
        total_margin=account.total_margin,
        alloc_pct=allocation.alloc_pct,
        committed_notional=Decimal(committed),
        pair_max_allocation_pct=pair_pct,
        pair_budget=Decimal(committed) * pair_pct,
        target=allocation.target,
        stop=allocation.stop,
        time_limit=allocation.time_limit,
        max_open_positions=allocation.max_open_positions,
    )
