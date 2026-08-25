"""Targeted database query for trade_id, symbols EWP/EWU, and account DUR919062."""

import asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings

async def main():
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as conn:
        print("=== 1. SEARCH TRADE_ID / SIGNAL_ID ===")
        res = await conn.execute(text(
            "SELECT * FROM signals WHERE trade_id LIKE '%EWP%' OR signal_id LIKE '%EWP%' OR pair LIKE '%EWP%'"
        ))
        rows = [dict(r) for r in res.mappings()]
        print(f"Signals matching EWP count: {len(rows)}")
        for r in rows:
            if "raw_payload" in r:
                del r["raw_payload"]
            print("Signal:", r)

        print("\n=== 2. SEARCH SIGNAL JOBS ===")
        res = await conn.execute(text(
            "SELECT * FROM signal_jobs WHERE trade_id LIKE '%EWP%' OR signal_id LIKE '%EWP%'"
        ))
        rows = [dict(r) for r in res.mappings()]
        print(f"Signal jobs matching EWP count: {len(rows)}")
        for r in rows:
            if "raw_payload" in r:
                del r["raw_payload"]
            if "capture_data" in r:
                del r["capture_data"]
            print("Signal job:", r)

        print("\n=== 3. SEARCH POSITIONS FOR EWP OR EWU ===")
        res = await conn.execute(text(
            "SELECT * FROM positions WHERE leg_a_symbol IN ('EWP', 'EWU') OR leg_b_symbol IN ('EWP', 'EWU')"
        ))
        rows = [dict(r) for r in res.mappings()]
        print(f"Positions matching EWP/EWU count: {len(rows)}")
        for r in rows:
            print("Position:", r)

        print("\n=== 4. COUNT OPEN POSITIONS PER ACCOUNT AND STRATEGY ===")
        res = await conn.execute(text(
            "SELECT account_id, strategy_id, risk_state, COUNT(*) FROM positions GROUP BY account_id, strategy_id, risk_state ORDER BY COUNT(*) DESC"
        ))
        for r in res.mappings():
            print(dict(r))

        print("\n=== 5. ACCOUNTS IN DB ===")
        res = await conn.execute(text(
            "SELECT * FROM accounts WHERE ibkr_account = 'DUR919062' OR name LIKE '%DUR919062%'"
        ))
        rows = [dict(r) for r in res.mappings()]
        print("Account DUR919062:", rows)

        res = await conn.execute(text(
            "SELECT * FROM accounts"
        ))
        rows = [dict(r) for r in res.mappings()]
        print(f"All accounts count: {len(rows)}")
        for r in rows[:10]:
            print(r)

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())
