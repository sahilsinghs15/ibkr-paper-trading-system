"""Check account allocations for model_blue."""

import asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.db.models.account import AccountModel
from app.db.models.strategy import AllocationModel, StrategyModel

async def main():
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as conn:
        stmt = (
            select(AccountModel, AllocationModel, StrategyModel)
            .join(AllocationModel, AllocationModel.account_id == AccountModel.id)
            .join(StrategyModel, StrategyModel.strategy_id == AllocationModel.strategy_id)
            .where(
                AllocationModel.strategy_id == "model_blue",
                AccountModel.enabled.is_(True),
                StrategyModel.enabled.is_(True),
                AllocationModel.enabled.is_(True),
            )
        )
        rows = (await conn.execute(stmt)).all()
        print(f"Allocations count for model_blue: {len(rows)}")
        for acc, alloc, strat in rows[:10]:
            print(f"Account ID: {acc.id}, IBKR Account: {acc.ibkr_account}, Name: {acc.name}, Alloc %: {alloc.alloc_pct}")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())
