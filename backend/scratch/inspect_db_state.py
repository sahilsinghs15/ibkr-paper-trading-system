"""List DB tables and contents safely."""

import asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings

async def inspect():
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as conn:
        print("=== TABLES IN DB ===")
        res = await conn.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'"))
        tables = [r[0] for r in res.fetchall()]
        print("Tables:", tables)

        for table in tables:
            print(f"\n--- Table: {table} ---")
            count_res = await conn.execute(text(f"SELECT COUNT(*) FROM {table}"))
            count = count_res.scalar()
            print(f"Row count: {count}")
            if count > 0:
                rows_res = await conn.execute(text(f"SELECT * FROM {table} LIMIT 10"))
                for row in rows_res.mappings():
                    r = dict(row)
                    if "raw_payload" in r:
                        r["raw_payload"] = "<truncated>"
                    if "capture_data" in r:
                        r["capture_data"] = "<truncated>"
                    print(r)

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(inspect())
