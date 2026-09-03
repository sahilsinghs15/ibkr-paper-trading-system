"""Delta-only directional margin-rate scanner."""

import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.oms.ibkr_adapter import WhatIfResult
from app.rms.margin_estimate import SOURCE_WHAT_IF
from app.services.margin_scanner import MarginScanner


def _settings(**kwargs) -> MagicMock:
    s = MagicMock()
    s.margin_scan_enabled = True
    s.margin_scan_max_per_sec = 50.0
    s.margin_scan_probe_notional = Decimal(1000)
    s.margin_scan_signal_lookback_days = 30
    s.margin_rate_refresh_sec = 300
    s.margin_rate_max_age_days = 7
    for key, value in kwargs.items():
        setattr(s, key, value)
    return s


def _scanner(*, probe_result: WhatIfResult | None = None, busy: bool = False) -> MarginScanner:
    adapter = MagicMock()
    adapter._client = MagicMock()
    adapter.probe_margin = AsyncMock(
        return_value=probe_result
        or WhatIfResult(
            order_id=1,
            unknown=False,
            init_margin_change=Decimal(150),
            rate=Decimal("0.15"),
        )
    )
    rate_service = MagicMock()
    rate_service.fresh_keys = AsyncMock(return_value=set())
    rate_service.upsert_rate = AsyncMock()
    pool = MagicMock()
    pool.has_in_flight_jobs.return_value = busy
    return MarginScanner(
        session_factory=MagicMock(),
        adapter=adapter,
        rate_service=rate_service,
        worker_pool=pool,
        account_for_probe=lambda: "DU1",
    )


@pytest.mark.asyncio
async def test_delta_only_skips_fresh_rows() -> None:
    scanner = _scanner()
    scanner._rate_service.fresh_keys = AsyncMock(return_value={("AAPL", "STK", "BUY")})
    with (
        patch("app.services.margin_scanner.get_settings", return_value=_settings()),
        patch.object(scanner, "_working_set", AsyncMock(return_value={("AAPL", "STK")})),
        patch.object(scanner, "_prices_for", AsyncMock(return_value={"AAPL": Decimal(100)})),
        patch.object(scanner, "_record_run", AsyncMock()),
        patch.object(scanner, "_resolve", AsyncMock(return_value=MagicMock())),
    ):
        counts = await scanner.run_scan(budget_sec=None)
    assert counts["planned"] == 1
    assert scanner._adapter.probe_margin.await_count == 1
    assert scanner._adapter.probe_margin.await_args.kwargs["side"] == "SELL"


@pytest.mark.asyncio
async def test_both_sides_written_independently() -> None:
    scanner = _scanner()
    with (
        patch("app.services.margin_scanner.get_settings", return_value=_settings()),
        patch.object(scanner, "_working_set", AsyncMock(return_value={("XLE", "STK")})),
        patch.object(scanner, "_prices_for", AsyncMock(return_value={"XLE": Decimal(50)})),
        patch.object(scanner, "_record_run", AsyncMock()),
        patch.object(scanner, "_resolve", AsyncMock(return_value=MagicMock())),
    ):
        counts = await scanner.run_scan(budget_sec=None)
    assert counts["written"] == 2
    sides = {c.kwargs["side"] for c in scanner._rate_service.upsert_rate.await_args_list}
    assert sides == {"BUY", "SELL"}
    assert all(
        c.kwargs["source"] == SOURCE_WHAT_IF
        for c in scanner._rate_service.upsert_rate.await_args_list
    )


@pytest.mark.asyncio
async def test_unknown_writes_no_row() -> None:
    scanner = _scanner(probe_result=WhatIfResult(order_id=1, unknown=True))
    with (
        patch("app.services.margin_scanner.get_settings", return_value=_settings()),
        patch.object(scanner, "_working_set", AsyncMock(return_value={("XLE", "STK")})),
        patch.object(scanner, "_prices_for", AsyncMock(return_value={"XLE": Decimal(50)})),
        patch.object(scanner, "_record_run", AsyncMock()),
        patch.object(scanner, "_resolve", AsyncMock(return_value=MagicMock())),
    ):
        counts = await scanner.run_scan(budget_sec=None)
    assert counts["unknown"] == 2
    assert counts["written"] == 0
    scanner._rate_service.upsert_rate.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_budget_defers_remainder() -> None:
    scanner = _scanner()
    with (
        patch("app.services.margin_scanner.get_settings", return_value=_settings()),
        patch.object(
            scanner, "_working_set", AsyncMock(return_value={("AAPL", "STK"), ("MSFT", "STK")})
        ),
        patch.object(
            scanner,
            "_prices_for",
            AsyncMock(return_value={"AAPL": Decimal(10), "MSFT": Decimal(10)}),
        ),
        patch.object(scanner, "_record_run", AsyncMock()),
        patch.object(scanner, "_resolve", AsyncMock(return_value=MagicMock())),
    ):
        counts = await scanner.run_scan(budget_sec=0)
    assert counts["planned"] == 4
    assert counts["probed"] == 0
    assert counts["skipped"] == 4


@pytest.mark.asyncio
async def test_pace_respects_max_per_sec() -> None:
    scanner = _scanner()
    with (
        patch(
            "app.services.margin_scanner.get_settings",
            return_value=_settings(margin_scan_max_per_sec=5.0),
        ),
        patch.object(scanner, "_working_set", AsyncMock(return_value={("AAPL", "STK")})),
        patch.object(scanner, "_prices_for", AsyncMock(return_value={"AAPL": Decimal(10)})),
        patch.object(scanner, "_record_run", AsyncMock()),
        patch.object(scanner, "_resolve", AsyncMock(return_value=MagicMock())),
    ):
        started = time.monotonic()
        await scanner.run_scan(budget_sec=None)
        elapsed = time.monotonic() - started
    assert elapsed >= 0.15
    assert scanner._adapter.probe_margin.await_count == 2


@pytest.mark.asyncio
async def test_skips_when_pool_busy() -> None:
    scanner = _scanner(busy=True)
    with (
        patch("app.services.margin_scanner.get_settings", return_value=_settings()),
        patch.object(scanner, "_working_set", AsyncMock(return_value={("AAPL", "STK")})),
        patch.object(scanner, "_prices_for", AsyncMock(return_value={"AAPL": Decimal(10)})),
        patch.object(scanner, "_record_run", AsyncMock()),
    ):
        counts = await scanner.run_scan(budget_sec=None)
    assert counts["skipped"] == 2
    scanner._adapter.probe_margin.assert_not_awaited()


@pytest.mark.asyncio
async def test_opens_no_market_data_subscriptions() -> None:
    scanner = _scanner()
    with (
        patch("app.services.margin_scanner.get_settings", return_value=_settings()),
        patch.object(scanner, "_working_set", AsyncMock(return_value={("AAPL", "STK")})),
        patch.object(scanner, "_prices_for", AsyncMock(return_value={"AAPL": Decimal(10)})),
        patch.object(scanner, "_record_run", AsyncMock()),
        patch.object(scanner, "_resolve", AsyncMock(return_value=MagicMock())),
    ):
        await scanner.run_scan(budget_sec=None)
    scanner._adapter._client.reqMktData.assert_not_called()


@pytest.mark.asyncio
async def test_run_recorded_to_event_log() -> None:
    scanner = _scanner()
    with (
        patch("app.services.margin_scanner.get_settings", return_value=_settings()),
        patch.object(scanner, "_working_set", AsyncMock(return_value=set())),
        patch.object(scanner, "_prices_for", AsyncMock(return_value={})),
        patch.object(scanner, "_record_run", AsyncMock()) as record,
    ):
        counts = await scanner.run_scan(budget_sec=None)
    record.assert_awaited_once_with(counts)
    assert counts["planned"] == 0
