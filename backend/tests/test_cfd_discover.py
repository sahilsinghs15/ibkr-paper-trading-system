"""Tests for CFD conId discovery helpers."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.instruments.cfd_discover import (
    cfd_search_contract,
    instrument_record_from_details,
    pick_unique_cfd_details,
)
from app.instruments.models import InstrumentRecord


def _details(symbol: str, con_id: int, *, exchange: str = "SMART", currency: str = "USD"):
    contract = SimpleNamespace(
        symbol=symbol,
        secType="CFD",
        conId=con_id,
        exchange=exchange,
        currency=currency,
        primaryExchange="ARCA",
        multiplier="1",
    )
    return SimpleNamespace(contract=contract, minSize=1, multiplier=1)


def test_cfd_search_contract_defaults() -> None:
    contract = cfd_search_contract("xle")
    assert contract.symbol == "XLE"
    assert contract.secType == "CFD"
    assert contract.exchange == "SMART"
    assert contract.currency == "USD"


def test_pick_unique_cfd_details_single_match() -> None:
    picked = pick_unique_cfd_details([_details("XLE", 12345)])
    assert picked is not None
    assert picked.contract.conId == 12345


def test_pick_unique_cfd_details_ambiguous() -> None:
    rows = [
        _details("XLE", 1, exchange="SMART"),
        _details("XLE", 2, exchange="SMART"),
    ]
    assert pick_unique_cfd_details(rows) is None


def test_pick_unique_cfd_details_prefers_smart() -> None:
    rows = [
        _details("XLE", 1, exchange="NYSE"),
        _details("XLE", 2, exchange="SMART"),
    ]
    picked = pick_unique_cfd_details(rows)
    assert picked is not None
    assert picked.contract.conId == 2


def test_instrument_record_from_details() -> None:
    record = instrument_record_from_details(_details("XLE", 999))
    assert record is not None
    assert record.symbol == "XLE"
    assert record.sec_type == "CFD"
    assert record.trade_conid == 999
    assert record.market_data_conid == 999


@pytest.mark.asyncio
async def test_discover_and_upsert_cfd_upserts_unique_match(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.instruments.cfd_discover import discover_and_upsert_cfd

    client = MagicMock()
    client.is_connected.return_value = True
    client.request_contract_details.return_value = [_details("XLE", 424242)]

    saved_holder: list[InstrumentRecord] = []

    class FakeRepo:
        def __init__(self, _session) -> None:
            pass

        async def upsert(self, record: InstrumentRecord) -> InstrumentRecord:
            saved_holder.append(record)
            return record

    monkeypatch.setattr(
        "app.db.repositories.instrument_repository.InstrumentRepository",
        FakeRepo,
    )

    session = AsyncMock()
    begin_ctx = AsyncMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=None)
    begin_ctx.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=begin_ctx)
    session_ctx = AsyncMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=None)
    session_factory = MagicMock(return_value=session_ctx)

    row = await discover_and_upsert_cfd(
        symbol="XLE",
        client=client,
        session_factory=session_factory,
    )
    assert row is not None
    assert row.trade_conid == 424242
    assert saved_holder[0].symbol == "XLE"


@pytest.mark.asyncio
async def test_ensure_cfd_skips_when_catalog_has_row() -> None:
    from app.instruments.cfd_discover import ensure_cfd_instruments_for_symbols

    catalog = MagicMock()
    catalog.find_all_async = AsyncMock(
        return_value=[
            InstrumentRecord(
                symbol="XLE",
                sec_type="CFD",
                trade_conid=1,
                market_data_conid=1,
                exchange="SMART",
                currency="USD",
                multiplier=Decimal(1),
            )
        ]
    )
    client = MagicMock()
    out = await ensure_cfd_instruments_for_symbols(
        symbols=["XLE"],
        client=client,
        session_factory=MagicMock(),
        catalog=catalog,
    )
    assert out == []
    client.request_contract_details.assert_not_called()
