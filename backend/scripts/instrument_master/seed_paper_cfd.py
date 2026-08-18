"""Upsert verified paper CFD catalog rows (SIL, GDX). Does not alter trading history."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_backend_dir = str(Path(__file__).resolve().parent.parent.parent)
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from app.db.repositories.instrument_repository import InstrumentRepository
from app.db.session import AsyncSessionLocal
from app.instruments.paper_cfd_catalog import PAPER_CFD_INSTRUMENTS


async def seed_paper_cfd_instruments() -> list[str]:
    seeded: list[str] = []
    async with AsyncSessionLocal() as session, session.begin():
        repo = InstrumentRepository(session)
        for record in PAPER_CFD_INSTRUMENTS:
            saved = await repo.upsert(record)
            seeded.append(
                f"{saved.symbol} sec_type={saved.sec_type} trade_conid={saved.trade_conid}"
            )
    return seeded


def main() -> None:
    rows = asyncio.run(seed_paper_cfd_instruments())
    for row in rows:
        print(row)


if __name__ == "__main__":
    main()
