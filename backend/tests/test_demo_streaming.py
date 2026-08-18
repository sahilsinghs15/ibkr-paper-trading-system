"""Demo streaming helpers. No IBKR, no order placement."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from demo_streaming.snapshot import classify_event, fingerprint, position_leg_payloads
from demo_streaming.publisher import PositionBridge


class _Pos:
    account_id = 7
    trade_id = "MBG-PAPER-DEMO"
    strategy_id = "model_blue"
    leg_a_symbol = "SIL"
    leg_a_signed_qty = Decimal("275")
    leg_a_entry_mark = Decimal("88.3900")
    leg_a_instrument_type = "CFD"
    leg_b_symbol = "GDX"
    leg_b_signed_qty = Decimal("-270")
    leg_b_entry_mark = Decimal("90.0200")
    leg_b_instrument_type = "CFD"
    live_pnl = Decimal("0")
    realised_pnl = Decimal("0")
    commission = Decimal("0")
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

