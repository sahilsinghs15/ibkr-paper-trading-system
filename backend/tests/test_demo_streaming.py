"""Demo streaming helpers. No IBKR, no order placement."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from demo_streaming.publisher import PositionBridge
from demo_streaming.snapshot import classify_event, fingerprint, position_leg_payloads


class _Pos:
    account_id = 7
    trade_id = "MBG-PAPER-DEMO"
    strategy_id = "model_blue"
    leg_a_symbol = "SIL"
    leg_a_signed_qty = Decimal(275)
    leg_a_entry_mark = Decimal("88.3900")
    leg_a_instrument_type = "CFD"
    leg_b_symbol = "GDX"
    leg_b_signed_qty = Decimal(-270)
    leg_b_entry_mark = Decimal("90.0200")
    leg_b_instrument_type = "CFD"
    live_pnl = Decimal(0)
    realised_pnl = Decimal(0)
    commission = Decimal(0)
    risk_state = "OPEN"


class _Acct:
    ibkr_account = "DUR919062"
    name = "paper"


class FakeStream:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def xadd(self, payload: dict) -> str:
        self.events.append(payload)
        return str(len(self.events))


def test_cfd_instrument_type_is_preserved() -> None:
    rows = position_leg_payloads(_Pos(), _Acct(), [], [], timestamp=datetime.now(UTC))
    assert {row["symbol"]: row["instrument_type"] for row in rows} == {
        "SIL": "CFD",
        "GDX": "CFD",
    }
    assert rows[0]["market_data_status"] == "UNAVAILABLE"
    assert rows[0]["side"] == "BUY"
    assert rows[1]["side"] == "SELL"


def test_event_classification() -> None:
    assert classify_event(
        previous_status=None,
        current_status="OPEN",
        previous_fill=None,
        current_fill="275",
        close_in_progress=False,
    ) == "POSITION_OPEN"
    assert classify_event(
        previous_status="OPEN",
        current_status="OPEN",
        previous_fill="100",
        current_fill="275",
        close_in_progress=False,
    ) == "POSITION_UPDATE"
    assert classify_event(
        previous_status="OPEN",
        current_status="OPEN",
        previous_fill="275",
        current_fill="100",
        close_in_progress=True,
    ) == "POSITION_PARTIAL_CLOSE"
    assert classify_event(
        previous_status="OPEN",
        current_status="CLOSED",
        previous_fill="275",
        current_fill="275",
        close_in_progress=False,
    ) == "POSITION_CLOSED"


def test_fingerprint_changes_on_pnl() -> None:
    base = {"status": "OPEN", "filled_quantity": "275", "unrealized_pnl": "0"}
    other = dict(base)
    other["unrealized_pnl"] = "1.5"
    assert fingerprint(base) != fingerprint(other)


@pytest.mark.asyncio
async def test_bridge_does_not_replay_baseline_then_emits_close() -> None:
    class _CM:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return None

    class _Factory:
        def __call__(self):
            return _CM()

    stream = FakeStream()
    bridge = PositionBridge(session_factory=_Factory(), stream=stream, poll_interval=0.01)  # type: ignore[arg-type]
    open_payload = position_leg_payloads(_Pos(), _Acct(), [], [], timestamp=datetime.now(UTC))[0]
    closed_pos = _Pos()
    closed_pos.risk_state = "CLOSED"
    closed_pos.realised_pnl = Decimal("-48.0251")
    closed_payload = position_leg_payloads(closed_pos, _Acct(), [], [], timestamp=datetime.now(UTC))[0]

    async def first(_session=None):
        return [open_payload]

    async def second(_session=None):
        return [closed_payload]

    bridge._collect = first  # type: ignore[method-assign]
    await bridge.restore_baseline()
    assert stream.events == []
    emitted = await bridge.poll_once()
    assert emitted == []
    bridge._collect = second  # type: ignore[method-assign]
    emitted = await bridge.poll_once()
    assert emitted
    assert emitted[0]["event"] == "POSITION_CLOSED"
    assert emitted[0]["instrument_type"] == "CFD"


@pytest.mark.asyncio
async def test_bridge_emits_position_update_when_live_pnl_changes() -> None:
    class _CM:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return None

    class _Factory:
        def __call__(self):
            return _CM()

    stream = FakeStream()
    bridge = PositionBridge(session_factory=_Factory(), stream=stream, poll_interval=0.01)  # type: ignore[arg-type]
    open_zero = position_leg_payloads(_Pos(), _Acct(), [], [], timestamp=datetime.now(UTC))[0]
    updated_pos = _Pos()
    updated_pos.live_pnl = Decimal(492)
    open_live = position_leg_payloads(updated_pos, _Acct(), [], [], timestamp=datetime.now(UTC))[0]

    async def first(_session=None):
        return [open_zero]

    async def second(_session=None):
        return [open_live]

    bridge._collect = first  # type: ignore[method-assign]
    await bridge.restore_baseline()
    bridge._collect = second  # type: ignore[method-assign]
    emitted = await bridge.poll_once()
    assert emitted
    assert emitted[0]["event"] == "POSITION_UPDATE"
    assert emitted[0]["unrealized_pnl"] == "492"
    assert emitted[0]["trade_id"] == "MBG-PAPER-DEMO"
    assert emitted[0]["instrument_type"] == "CFD"


@pytest.mark.asyncio
async def test_vanished_open_row_uses_closed_realised_pnl() -> None:
    class _CM:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return None

    class _Factory:
        def __call__(self):
            return _CM()

    stream = FakeStream()
    bridge = PositionBridge(session_factory=_Factory(), stream=stream, poll_interval=0.01)  # type: ignore[arg-type]
    open_payload = position_leg_payloads(_Pos(), _Acct(), [], [], timestamp=datetime.now(UTC))[0]
    closed_pos = _Pos()
    closed_pos.risk_state = "CLOSED"
    closed_pos.realised_pnl = Decimal("-48.0251")
    closed_pos.commission = Decimal("1.25")
    closed_pos.live_pnl = Decimal("-48.0251")
    closed_payload = position_leg_payloads(closed_pos, _Acct(), [], [], timestamp=datetime.now(UTC))[0]

    async def first(_session=None):
        return [open_payload]

    async def empty(_session=None):
        return []

    async def enrich(_keys):
        return {
            (7, "MBG-PAPER-DEMO", "SIL"): closed_payload,
        }

    bridge._collect = first  # type: ignore[method-assign]
    await bridge.restore_baseline()
    bridge._collect = empty  # type: ignore[method-assign]
    bridge._payloads_for_vanished = enrich  # type: ignore[method-assign]
    emitted = await bridge.poll_once()
    assert emitted
    assert emitted[0]["event"] == "POSITION_CLOSED"
    assert emitted[0]["realized_pnl"] == "-48.0251"
    assert emitted[0]["commission"] == "1.25"
    assert emitted[0]["status"] == "CLOSED"



def test_demo_ui_timezone_and_no_market_data_warning() -> None:
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "demo_streaming" / "static" / "index.html").read_text()
    assert "America/New_York" in html
    assert "Asia/Kolkata" in html
    assert "modelBlue.displayTimezone" in html
    assert "id=\"tzNy\"" in html
    assert "id=\"tzIn\"" in html
    assert "MARKET DATA" not in html
    assert "mdDot" not in html
    assert "PAPER" in html
    assert "streamDot" in html
    assert "if (raw === \"STK\") return \"CFD\"" in html
    assert "function fmtUsd" in html
    assert "function fmtPnl" in html
    assert "+$" in html
    assert "₹" not in html
    assert "INR" not in html
    assert "America/New_York" in html
    assert "MARKET DATA" not in html
    assert "colspan=\"6\"" in html
    assert 'class="right dim">—' in html


def test_demo_usd_and_pnl_display_contract() -> None:
    def fmt_usd(v):
        if v is None or v == "":
            return "—"
        return f"${float(v):,.2f}"

    def fmt_pnl(v):
        if v is None or v == "":
            return "—"
        n = float(v)
        if n > 0:
            return f"+${n:,.2f}"
        if n < 0:
            return f"-${abs(n):,.2f}"
        return f"${n:,.2f}"

    assert fmt_pnl(492) == "+$492.00"
    assert fmt_pnl(-312.517801) == "-$312.52"
    assert fmt_pnl(0) == "$0.00"
    assert fmt_pnl(None) == "—"
    assert fmt_pnl(984) == "+$984.00"
    assert fmt_pnl(-125.5) == "-$125.50"
    assert fmt_usd(90.64) == "$90.64"
    assert fmt_usd(87.930727) == "$87.93"
    assert fmt_usd(None) == "—"
    assert "₹" not in fmt_usd(90.64) + fmt_pnl(492)


def test_open_position_leg_payload_realized_pnl_is_none() -> None:
    open_pos = _Pos()
    open_pos.risk_state = "OPEN"
    open_pos.realised_pnl = Decimal(0)
    open_pos.live_pnl = Decimal("125.40")

    legs = position_leg_payloads(open_pos, _Acct(), [], [], timestamp=datetime.now(UTC))
    assert legs[0]["unrealized_pnl"] == "125.40"
    assert legs[0]["realized_pnl"] is None

    closed_pos = _Pos()
    closed_pos.risk_state = "CLOSED"
    closed_pos.realised_pnl = Decimal("492.00")
    closed_pos.live_pnl = Decimal("492.00")

    closed_legs = position_leg_payloads(closed_pos, _Acct(), [], [], timestamp=datetime.now(UTC))
    assert closed_legs[0]["unrealized_pnl"] is None
    assert closed_legs[0]["realized_pnl"] == "492.00"


@pytest.mark.asyncio
async def test_load_signals_handles_none_session_safely() -> None:
    from demo_streaming.snapshot import load_signals

    res = await load_signals(None)  # type: ignore[arg-type]
    assert res == []


@pytest.mark.asyncio
async def test_signal_status_terminal_classification() -> None:
    from app.db.repositories.signal_repository import (
        SIGNAL_STATUS_NEW,
        SIGNAL_STATUS_PROCESSED,
        SIGNAL_STATUS_REJECTED,
    )

    statuses = [SIGNAL_STATUS_NEW, SIGNAL_STATUS_PROCESSED, SIGNAL_STATUS_REJECTED]
    assert "PROCESSED" in statuses
    assert "REJECTED" in statuses
    assert "NEW" in statuses




