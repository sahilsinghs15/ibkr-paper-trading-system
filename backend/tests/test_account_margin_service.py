"""AccountMarginService: reqAccountSummary snapshot, inf guard, disconnect."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

from app.services.account_margin import (
    MARGIN_TAGS,
    AccountMarginService,
    AccountMarginSnapshot,
    parse_ibkr_number,
)


def _client() -> MagicMock:
    tws = MagicMock()
    tws.is_connected.return_value = True
    return tws


def _req_id(svc: AccountMarginService, tws: MagicMock) -> int:
    svc.start()
    tws.reqAccountSummary.assert_called_once()
    return tws.reqAccountSummary.call_args.args[0]


def test_parse_ibkr_number_rejects_inf_and_double_max() -> None:
    assert parse_ibkr_number("inf") is None
    assert parse_ibkr_number("-inf") is None
    assert parse_ibkr_number("nan") is None
    assert parse_ibkr_number("1.7976931348623157E+308") is None
    assert parse_ibkr_number("12,345.50") == Decimal("12345.50")


def test_two_accounts_from_all_subscription() -> None:
    tws = _client()
    svc = AccountMarginService(tws, max_age_sec=300)
    req_id = _req_id(svc, tws)
    group, tags = tws.reqAccountSummary.call_args.args[1], tws.reqAccountSummary.call_args.args[2]
    assert group == "All"
    assert "LookAheadAvailableFunds" in tags
    assert tags == MARGIN_TAGS

    svc.on_account_summary(req_id, "dua", "NetLiquidation", "100000", "USD")
    svc.on_account_summary(req_id, "dua", "AvailableFunds", "40000", "USD")
    svc.on_account_summary(req_id, "dua", "LookAheadAvailableFunds", "35000", "USD")
    svc.on_account_summary(req_id, "dub", "NetLiquidation", "200000", "USD")
    svc.on_account_summary(req_id, "dub", "AvailableFunds", "80000", "USD")
    svc.on_account_summary(req_id, "dub", "LookAheadAvailableFunds", "70000", "USD")
    svc.on_account_summary_end(req_id)

    a = svc.snapshot_for("DUA")
    b = svc.snapshot_for("dub")
    assert a is not None and b is not None
    assert a.net_liquidation == Decimal(100000)
    assert a.available_funds == Decimal(40000)
    assert a.look_ahead_available_funds == Decimal(35000)
    assert b.net_liquidation == Decimal(200000)
    assert b.look_ahead_available_funds == Decimal(70000)
    assert set(svc.all_snapshots()) == {"DUA", "DUB"}


def test_inf_and_double_max_become_none() -> None:
    tws = _client()
    svc = AccountMarginService(tws, max_age_sec=300)
    req_id = _req_id(svc, tws)
    svc.on_account_summary(req_id, "DU1", "AvailableFunds", "inf", "USD")
    svc.on_account_summary(req_id, "DU1", "ExcessLiquidity", "-inf", "USD")
    svc.on_account_summary(req_id, "DU1", "BuyingPower", "1.7976931348623157E+308", "USD")
    svc.on_account_summary(req_id, "DU1", "NetLiquidation", "50000", "USD")
    svc.on_account_summary_end(req_id)
    snap = svc.snapshot_for("DU1")
    assert snap is not None
    assert snap.available_funds is None
    assert snap.excess_liquidity is None
    assert snap.buying_power is None
    assert snap.net_liquidation == Decimal(50000)


def test_connection_closed_clears_cache() -> None:
    tws = _client()
    svc = AccountMarginService(tws, max_age_sec=300)
    req_id = _req_id(svc, tws)
    svc.on_account_summary(req_id, "DU1", "AvailableFunds", "10", "USD")
    svc.on_account_summary_end(req_id)
    assert svc.snapshot_for("DU1") is not None
    svc.on_connection_closed()
    assert svc.snapshot_for("DU1") is None
    assert svc.all_snapshots() == {}


def test_is_stale_flips_at_max_age() -> None:
    fresh = AccountMarginSnapshot(
        ibkr_account="DU1",
        as_of=datetime.now(UTC),
        available_funds=Decimal(1),
        max_age_sec=5,
    )
    stale = AccountMarginSnapshot(
        ibkr_account="DU1",
        as_of=datetime.now(UTC) - timedelta(seconds=6),
        available_funds=Decimal(1),
        max_age_sec=5,
    )
    missing = AccountMarginSnapshot(ibkr_account="DU1", as_of=None, max_age_sec=5)
    assert fresh.is_stale is False
    assert stale.is_stale is True
    assert missing.is_stale is True


def test_snapshot_listener_fires_per_account() -> None:
    tws = _client()
    svc = AccountMarginService(tws, max_age_sec=300)
    seen: list[str] = []
    svc.add_snapshot_listener(lambda snap: seen.append(snap.ibkr_account))
    req_id = _req_id(svc, tws)
    svc.on_account_summary(req_id, "A", "AvailableFunds", "1", "USD")
    svc.on_account_summary(req_id, "B", "AvailableFunds", "2", "USD")
    svc.on_account_summary_end(req_id)
    assert seen == ["A", "B"]
