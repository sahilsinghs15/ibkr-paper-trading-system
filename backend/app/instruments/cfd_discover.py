"""Discover IBKR CFD conIds via reqContractDetails and upsert instruments master."""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from app.instruments.models import InstrumentRecord

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.broker.ibkr.tws_client import TWSClient

logger = logging.getLogger(__name__)

_DEFAULT_EXCHANGE = "SMART"
_DEFAULT_CURRENCY = "USD"


def cfd_search_contract(
    symbol: str,
    *,
    exchange: str | None = None,
    currency: str | None = None,
) -> Any:
    """Build an ibapi Contract for CFD contract-details lookup."""
    from ibapi.contract import Contract  # type: ignore[import-untyped]

    contract = Contract()
    contract.symbol = symbol.strip().upper()
    contract.secType = "CFD"
    contract.exchange = (exchange or "").strip() or _DEFAULT_EXCHANGE
    contract.currency = (currency or "").strip() or _DEFAULT_CURRENCY
    return contract


def pick_unique_cfd_details(details: list[Any]) -> Any | None:
    """Return one ContractDetails when exactly one USD CFD match remains."""
    cfd_rows = []
    for row in details:
        contract = getattr(row, "contract", None)
        if contract is None:
            continue
        if (getattr(contract, "secType", "") or "").upper() != "CFD":
            continue
        currency = (getattr(contract, "currency", "") or "").upper()
        if currency and currency != "USD":
            continue
        cfd_rows.append(row)

    if not cfd_rows:
        return None

    smart = [
        row
        for row in cfd_rows
        if (getattr(getattr(row, "contract", None), "exchange", "") or "").upper()
        == "SMART"
    ]
    pool = smart or cfd_rows
    if len(pool) != 1:
        return None
    return pool[0]


def instrument_record_from_details(details: Any) -> InstrumentRecord | None:
    """Map IBKR ContractDetails to InstrumentRecord. Does not invent conIds."""
    contract = getattr(details, "contract", None)
    if contract is None:
        return None
    con_id = int(getattr(contract, "conId", 0) or 0)
    if con_id <= 0:
        return None
    symbol = (getattr(contract, "symbol", "") or "").strip()
    if not symbol:
        return None
    exchange = (getattr(contract, "exchange", "") or "").strip() or _DEFAULT_EXCHANGE
    currency = (getattr(contract, "currency", "") or "").strip() or _DEFAULT_CURRENCY
    primary = getattr(contract, "primaryExchange", None) or getattr(
        details, "underSymbol", None
    )
    multiplier_raw = getattr(contract, "multiplier", None) or getattr(
        details, "multiplier", None
    )
    try:
        multiplier = Decimal(str(multiplier_raw)) if multiplier_raw else Decimal(1)
    except (InvalidOperation, ValueError):
        multiplier = Decimal(1)
    min_size = getattr(details, "minSize", None)
    try:
        size_increment = Decimal(str(min_size)) if min_size else Decimal(1)
    except (InvalidOperation, ValueError):
        size_increment = Decimal(1)
    underlying_ex = (primary or exchange or _DEFAULT_EXCHANGE).strip()
    return InstrumentRecord(
        symbol=symbol,
        sec_type="CFD",
        trade_conid=con_id,
        market_data_conid=con_id,
        exchange=exchange,
        currency=currency,
        multiplier=multiplier if multiplier > 0 else Decimal(1),
        underlying_exchange=underlying_ex,
        size_increment=size_increment if size_increment > 0 else Decimal(1),
    )


async def discover_and_upsert_cfd(
    *,
    symbol: str,
    client: TWSClient | None,
    session_factory: async_sessionmaker[AsyncSession],
    market: str | None = None,
    currency: str | None = None,
    timeout: float = 5.0,
) -> InstrumentRecord | None:
    """Discover CFD conId from Gateway and upsert instruments. Best-effort."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return None
    if client is None or not client.is_connected():
        logger.warning(
            "CFD discover skipped: symbol=%s reason=TWS not connected",
            sym,
        )
        return None

    contract = cfd_search_contract(sym, exchange=market, currency=currency)
    req_async = getattr(client, "request_contract_details_async", None)
    if callable(req_async):
        details = await req_async(contract, timeout=timeout)
    else:
        details = await asyncio.to_thread(client.request_contract_details, contract, timeout=timeout)
    picked = pick_unique_cfd_details(details)
    if picked is None:
        count = len(
            [
                row
                for row in details
                if (getattr(getattr(row, "contract", None), "secType", "") or "").upper()
                == "CFD"
            ]
        )
        if count == 0:
            logger.warning(
                "CFD discover: no CFD contract details for symbol=%s",
                sym,
            )
        else:
            logger.warning(
                "CFD discover: ambiguous CFD matches for symbol=%s count=%d",
                sym,
                count,
            )
        return None

    record = instrument_record_from_details(picked)
    if record is None:
        logger.warning("CFD discover: could not map contract details for symbol=%s", sym)
        return None

    from app.db.repositories.instrument_repository import InstrumentRepository

    async with session_factory() as session, session.begin():
        saved = await InstrumentRepository(session).upsert(record)
    logger.info(
        "CFD discover upserted: symbol=%s trade_conid=%s exchange=%s",
        saved.symbol,
        saved.trade_conid,
        saved.exchange,
    )
    return saved


async def ensure_cfd_instruments_for_symbols(
    *,
    symbols: list[str],
    client: TWSClient | None,
    session_factory: async_sessionmaker[AsyncSession],
    catalog: Any,
    market: str | None = None,
    currency: str | None = None,
    timeout: float = 5.0,
) -> list[InstrumentRecord]:
    """Discover missing CFD rows for symbols. Does not block on failure."""
    discovered: list[InstrumentRecord] = []
    for symbol in symbols:
        sym = (symbol or "").strip().upper()
        if not sym:
            continue
        finder = getattr(catalog, "find_all_async", None)
        if callable(finder):
            existing = list(await finder(sym, "CFD"))
        else:
            existing = list(catalog.find_all(sym, "CFD"))
        if existing:
            continue
        row = await discover_and_upsert_cfd(
            symbol=sym,
            client=client,
            session_factory=session_factory,
            market=market,
            currency=currency,
            timeout=timeout,
        )
        if row is not None:
            discovered.append(row)
    return discovered
