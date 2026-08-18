"""Lookup against the existing instruments master. Does not invent conIds."""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.instrument import InstrumentModel
from app.instruments.models import InstrumentRecord
from app.instruments.resolver import InstrumentCatalog

SessionFactory = async_sessionmaker[AsyncSession]


def _to_record(row: InstrumentModel) -> InstrumentRecord:
    return InstrumentRecord(
        symbol=row.symbol,
        sec_type=row.sec_type,
        trade_conid=int(row.trade_conid),
        market_data_conid=int(row.market_data_conid) if row.market_data_conid else None,
        exchange=row.exchange,
        currency=row.currency,
        multiplier=row.multiplier,
        underlying_exchange=row.underlying_exchange,
    )


class DatabaseInstrumentCatalog:
    """Async-backed catalog; ``find_all`` is sync-incompatible so OMS uses preload or adapter path.

    OrderManager resolves intents with ``find_all_async`` before basket submit.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def find_all(self, symbol: str, sec_type: str) -> Sequence[InstrumentRecord]:
        raise RuntimeError(
            "DatabaseInstrumentCatalog.find_all is async-only; use find_all_async."
        )

    async def find_all_async(self, symbol: str, sec_type: str) -> Sequence[InstrumentRecord]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(InstrumentModel).where(
                    InstrumentModel.symbol == symbol,
                    InstrumentModel.sec_type == sec_type,
                )
            )
            return [_to_record(row) for row in result.scalars().all()]


class SnapshotInstrumentCatalog:
    """Sync catalog snapshot for a single intent (satisfies InstrumentCatalog)."""

    def __init__(self, rows: Sequence[InstrumentRecord]) -> None:
        self._rows = list(rows)

    def find_all(self, symbol: str, sec_type: str) -> Sequence[InstrumentRecord]:
        wanted_sym = symbol.strip().upper()
        wanted_sec = sec_type.strip().upper()
        return [
            row
            for row in self._rows
            if row.symbol.strip().upper() == wanted_sym
            and row.sec_type.strip().upper() == wanted_sec
        ]


# Protocol check for in-memory tests
def as_catalog(catalog: InstrumentCatalog) -> InstrumentCatalog:
    return catalog
