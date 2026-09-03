"""Delta-only directional margin-rate scanner.

Working set is allocation/signal/catalog symbols, not the universe. Probes
both BUY and SELL. Skips fresh rows. Uses a private token bucket plus a
worker-pool-busy skip because GatewayRateLimiter priority does not isolate
P4 from P1. Opens no market-data subscriptions.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.db.models.instrument import InstrumentModel
from app.db.models.signal import SignalModel
from app.db.models.strategy import AllocationModel
from app.db.repositories.event_repository import EventRepository
from app.instruments.models import ResolvedInstrument
from app.instruments.resolver import ibkr_contract_from_resolved, ibkr_sec_type
from app.oms.ibkr_adapter import IBKRExecutionAdapter, WhatIfResult
from app.rms.margin_estimate import SOURCE_WHAT_IF
from app.services.margin_rate import MarginRateService

logger = logging.getLogger(__name__)

_SIDES = ("BUY", "SELL")


class MarginScanner:
    """Measures init-margin rates via what-if probes and writes margin_rates."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        adapter: IBKRExecutionAdapter,
        rate_service: MarginRateService,
        *,
        worker_pool: Any | None = None,
        live_pnl: Any | None = None,
        account_for_probe: Callable[[], str | None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._adapter = adapter
        self._rate_service = rate_service
        self._worker_pool = worker_pool
        self._live_pnl = live_pnl
        self._account_for_probe = account_for_probe
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_probe_mono = 0.0

    async def start_background(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._background_loop(), name="margin-scanner")
        logger.info("MarginScanner background task started")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _background_loop(self) -> None:
        settings = get_settings()
        while self._running:
            try:
                if self._pool_busy():
                    await asyncio.sleep(1.0)
                    continue
                await self.run_scan(budget_sec=None)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("MarginScanner background scan failed")
            try:
                await asyncio.sleep(float(settings.margin_rate_refresh_sec))
            except asyncio.CancelledError:
                break

    def _pool_busy(self) -> bool:
        pool = self._worker_pool
        if pool is None:
            return False
        checker = getattr(pool, "has_in_flight_jobs", None)
        if callable(checker):
            return bool(checker())
        return False

    async def run_scan(self, *, budget_sec: float | None) -> dict[str, int]:
        """Run a delta scan. Returns planned/probed/written/unknown counts."""
        settings = get_settings()
        if not settings.margin_scan_enabled:
            return {"planned": 0, "probed": 0, "written": 0, "unknown": 0, "skipped": 0}

        deadline = (
            time.monotonic() + float(budget_sec) if budget_sec is not None else None
        )
        working = await self._working_set()
        prices = await self._prices_for(working)
        fresh = await self._rate_service.fresh_keys()
        planned: list[tuple[str, str, str, Decimal]] = []
        for symbol, itype in working:
            price = prices.get(symbol)
            if price is None or price <= 0:
                logger.info("Margin scan skip %s: no probe price", symbol)
                continue
            for side in _SIDES:
                key = (symbol, itype, side)
                if key in fresh:
                    continue
                planned.append((symbol, itype, side, price))

        logger.info("Margin scan planned probes=%d", len(planned))
        probed = 0
        written = 0
        unknown = 0
        skipped = 0
        remainder: list[tuple[str, str, str, Decimal]] = []
        account = self._probe_account()
        if not account:
            logger.warning("Margin scan skipped: no IBKR account for whatIf")
            return {
                "planned": len(planned),
                "probed": 0,
                "written": 0,
                "unknown": 0,
                "skipped": len(planned),
            }

        for symbol, itype, side, price in planned:
            if deadline is not None and time.monotonic() >= deadline:
                remainder.append((symbol, itype, side, price))
                continue
            if self._pool_busy():
                remainder.append((symbol, itype, side, price))
                continue
            await self._pace()
            result = await self._probe_one(symbol, itype, side, price, account)
            probed += 1
            if result is None or result.unknown or result.rate is None or result.rate <= 0:
                unknown += 1
                continue
            if result.rate > 1:
                logger.warning(
                    "Margin scan rate>1 for %s %s %s: %s; not writing",
                    symbol,
                    itype,
                    side,
                    result.rate,
                )
                unknown += 1
                continue
            qty = max(Decimal(1), (settings.margin_scan_probe_notional / price).to_integral_value())
            notional = qty * price
            await self._rate_service.upsert_rate(
                symbol=symbol,
                instrument_type=itype,
                side=side,
                rate=result.rate,
                probe_notional=notional,
                init_margin_change=result.init_margin_change or Decimal(0),
                source=SOURCE_WHAT_IF,
            )
            written += 1

        skipped = len(remainder)
        counts = {
            "planned": len(planned),
            "probed": probed,
            "written": written,
            "unknown": unknown,
            "skipped": skipped,
        }
        await self._record_run(counts)
        logger.info("Margin scan complete: %s", counts)
        return counts

    async def _pace(self) -> None:
        settings = get_settings()
        min_interval = 1.0 / float(settings.margin_scan_max_per_sec)
        now = time.monotonic()
        wait = min_interval - (now - self._last_probe_mono)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_probe_mono = time.monotonic()

    def _probe_account(self) -> str | None:
        if self._account_for_probe is not None:
            return self._account_for_probe()
        managed = getattr(self._adapter._client, "managed_accounts", None)
        if managed:
            return next(iter(managed), None)
        return None

    async def _probe_one(
        self,
        symbol: str,
        itype: str,
        side: str,
        price: Decimal,
        account: str,
    ) -> WhatIfResult | None:
        settings = get_settings()
        qty = max(Decimal(1), (settings.margin_scan_probe_notional / price).to_integral_value())
        resolved = await self._resolve(symbol, itype)
        if resolved is None:
            logger.info("Margin scan skip %s: unresolved contract", symbol)
            return None
        contract = ibkr_contract_from_resolved(resolved)
        try:
            return await self._adapter.probe_margin(
                contract=contract,
                side=side,
                quantity=qty,
                price=price,
                ibkr_account=account,
            )
        except Exception:
            logger.exception("whatIf probe failed symbol=%s side=%s", symbol, side)
            return WhatIfResult(order_id=0, unknown=True)

    async def _resolve(self, symbol: str, itype: str) -> ResolvedInstrument | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(InstrumentModel).where(
                        InstrumentModel.symbol == symbol,
                        InstrumentModel.sec_type == itype,
                    )
                )
            ).scalar_one_or_none()
            if row is None and itype != "CFD":
                row = (
                    await session.execute(
                        select(InstrumentModel).where(InstrumentModel.symbol == symbol)
                    )
                ).scalar_one_or_none()
        if row is None:
            return ResolvedInstrument(
                symbol=symbol,
                requested_instrument_type=itype,
                sec_type=itype,
                exchange="SMART",
                currency="USD",
            )
        return ResolvedInstrument(
            symbol=row.symbol,
            requested_instrument_type=row.sec_type,
            sec_type=row.sec_type,
            exchange=row.exchange,
            currency=row.currency,
            con_id=int(row.trade_conid) if row.trade_conid else None,
            market_data_con_id=int(row.market_data_conid) if row.market_data_conid else None,
            multiplier=row.multiplier,
            primary_exchange=row.underlying_exchange,
            size_increment=row.size_increment,
        )

    async def _working_set(self) -> set[tuple[str, str]]:
        settings = get_settings()
        lookback = datetime.now(UTC) - timedelta(
            days=settings.margin_scan_signal_lookback_days
        )
        symbols: set[tuple[str, str]] = set()
        async with self._session_factory() as session:
            instruments = (await session.execute(select(InstrumentModel))).scalars().all()
            for row in instruments:
                itype = (row.sec_type or "CFD").strip().upper()
                symbols.add((row.symbol.strip().upper(), itype))

            allocs = (
                await session.execute(
                    select(AllocationModel).where(AllocationModel.enabled.is_(True))
                )
            ).scalars().all()
            enabled = bool(allocs)

            signals = (
                await session.execute(
                    select(SignalModel).where(SignalModel.received_at >= lookback)
                )
            ).scalars().all()
            for sig in signals:
                pair = (sig.pair or "").strip()
                for part in pair.replace("-", "/").split("/"):
                    token = part.strip().upper()
                    if token:
                        symbols.add((token, "STK"))
                payload = sig.raw_payload or {}
                for leg in payload.get("legs") or payload.get("buckets") or []:
                    if not isinstance(leg, dict):
                        continue
                    token = str(leg.get("symbol") or "").strip().upper()
                    if not token:
                        continue
                    raw_type = str(leg.get("instrument_type") or "STK")
                    try:
                        itype = ibkr_sec_type(raw_type)
                    except Exception:
                        itype = "STK"
                    symbols.add((token, itype))
        if not enabled:
            logger.info("Margin scan: no enabled allocations; scanning catalog/signals only")
        return symbols

    async def _prices_for(self, working: set[tuple[str, str]]) -> dict[str, Decimal]:
        prices: dict[str, Decimal] = {}
        live = self._live_pnl
        marks = getattr(live, "_marks", None) if live is not None else None
        if isinstance(marks, dict):
            for key, mark in marks.items():
                if mark is None:
                    continue
                symbol = key[1] if isinstance(key, tuple) and len(key) > 1 else None
                if symbol:
                    prices[str(symbol).strip().upper()] = Decimal(str(mark))

        settings = get_settings()
        lookback = datetime.now(UTC) - timedelta(
            days=settings.margin_scan_signal_lookback_days
        )
        async with self._session_factory() as session:
            signals = (
                await session.execute(
                    select(SignalModel)
                    .where(SignalModel.received_at >= lookback)
                    .order_by(SignalModel.received_at.desc())
                )
            ).scalars().all()
        for sig in signals:
            pair = (sig.pair or "").replace("-", "/").split("/")
            if len(pair) >= 1 and pair[0].strip() and sig.ref_price_a:
                prices.setdefault(pair[0].strip().upper(), Decimal(str(sig.ref_price_a)))
            if len(pair) >= 2 and pair[1].strip() and sig.ref_price_b:
                prices.setdefault(pair[1].strip().upper(), Decimal(str(sig.ref_price_b)))
        return prices

    async def _record_run(self, counts: dict[str, int]) -> None:
        try:
            async with self._session_factory() as session, session.begin():
                await EventRepository(session).append(
                    process="margin_scan",
                    kind="MARGIN_SCAN_RUN",
                    detail=counts,
                )
        except Exception:
            logger.exception("Failed to persist margin scan run")
