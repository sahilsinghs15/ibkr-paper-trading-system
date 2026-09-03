"""Load directional margin rates into RMSContext."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.db.models.margin_rate import MarginRateModel
from app.rms.margin_estimate import SOURCE_WHAT_IF
from app.rms.models import MarginPolicy, RMSContext


class MarginRateService:
    """Reads/writes margin_rates and publishes them onto RMSContext."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def load_into(self, context: RMSContext, policy: MarginPolicy) -> None:
        settings = get_settings()
        cutoff = datetime.now(UTC) - timedelta(days=settings.margin_rate_max_age_days)
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(MarginRateModel).where(MarginRateModel.scanned_at >= cutoff)
                )
            ).scalars().all()
        rates: dict[tuple[str, str, str], Decimal] = {}
        sources: dict[tuple[str, str, str], str] = {}
        for row in rows:
            key = (
                row.symbol.strip().upper(),
                row.instrument_type.strip().upper(),
                row.side.strip().upper(),
            )
            rates[key] = row.rate
            sources[key] = row.source or SOURCE_WHAT_IF
        context.margin_rates = rates
        context.margin_rate_sources = sources

    async def upsert_rate(
        self,
        *,
        symbol: str,
        instrument_type: str,
        side: str,
        rate: Decimal,
        probe_notional: Decimal,
        init_margin_change: Decimal,
        source: str = SOURCE_WHAT_IF,
    ) -> None:
        now = datetime.now(UTC)
        values = {
            "symbol": symbol.strip().upper(),
            "instrument_type": instrument_type.strip().upper(),
            "side": side.strip().upper(),
            "rate": rate,
            "source": source,
            "probe_notional": probe_notional,
            "init_margin_change": init_margin_change,
            "scanned_at": now,
            "updated_at": now,
        }
        async with self._session_factory() as session, session.begin():
            stmt = (
                insert(MarginRateModel)
                .values(**values)
                .on_conflict_do_update(
                    constraint="uq_margin_rates_symbol_type_side",
                    set_={
                        "rate": values["rate"],
                        "source": values["source"],
                        "probe_notional": values["probe_notional"],
                        "init_margin_change": values["init_margin_change"],
                        "scanned_at": values["scanned_at"],
                        "updated_at": values["updated_at"],
                    },
                )
            )
            await session.execute(stmt)

    async def fresh_keys(self) -> set[tuple[str, str, str]]:
        settings = get_settings()
        cutoff = datetime.now(UTC) - timedelta(days=settings.margin_rate_max_age_days)
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        MarginRateModel.symbol,
                        MarginRateModel.instrument_type,
                        MarginRateModel.side,
                    ).where(MarginRateModel.scanned_at >= cutoff)
                )
            ).all()
        return {
            (r.symbol.strip().upper(), r.instrument_type.strip().upper(), r.side.strip().upper())
            for r in rows
        }
