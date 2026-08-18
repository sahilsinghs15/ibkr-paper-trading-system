"""Signal audit persistence: original webhook JSON, pair, side. No live broker."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.signal import SignalModel
from app.db.repositories.signal_repository import (
    SIGNAL_STATUS_NEW,
    SIGNAL_STATUS_PROCESSED,
    SignalRepository,
    original_raw_payload,
    persist_signal_id_for,
)
from app.db.session import create_engine_from_settings
from app.models.signal import Signal, SignalLeg, SignalType
from app.services.model_blue.parser import MODEL_BLUE_STRATEGY_ID, parse_model_blue_payload
from app.services.strategies.inbound import parse_tradingview_payload

_TS = datetime(2026, 8, 18, 16, 0, tzinfo=UTC)

SIL_GDX_PAYLOAD = {
    "market": "SMART",
    "strategy": "model_blue",
    "action": "OPEN",
    "trade_id": "MBG-PAPER-TEST-SIL-GDX",
    "direction": 1,
    "buckets": [
        {
            "underlying": "SIL",
            "legs": [
                {"instrument_type": "STK", "side": "BUY", "weight": 0.5019, "price": 90.64}
            ],
        },
        {
            "underlying": "GDX",
            "legs": [
                {
                    "instrument_type": "STK",
                    "side": "SELL",
                    "weight": -0.4981,
                    "price": 91.86,
                }
            ],
        },
    ],
}


@pytest.fixture
async def db_factory() -> async_sessionmaker[AsyncSession]:
    engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def test_original_raw_payload_keeps_capture_envelope() -> None:
    capture = {
        "metadata": {"request_id": "abc"},
        "raw_body": '{"strategy":"model_blue"}',
        "parsed_json": SIL_GDX_PAYLOAD,
    }
    signal = parse_tradingview_payload(
        SIL_GDX_PAYLOAD,
        timestamp=_TS,
        request_id="abc",
        capture_data=capture,
    )
    stored = original_raw_payload(signal)
    assert stored["parsed_json"]["trade_id"] == "MBG-PAPER-TEST-SIL-GDX"
    assert stored["parsed_json"]["buckets"][0]["underlying"] == "SIL"
    assert stored["raw_body"]
    assert stored != {}


def test_original_raw_payload_never_empty_without_capture() -> None:
    signal = parse_model_blue_payload(SIL_GDX_PAYLOAD, timestamp=_TS, reason="unit")
    stored = original_raw_payload(signal)
    assert stored != {}
    assert stored["trade_id"] == "MBG-PAPER-TEST-SIL-GDX"
    assert stored["buckets"][0]["underlying"] == "SIL"


def test_persist_signal_id_close_suffix() -> None:
    signal = Signal(
        signal_type=SignalType.SELL,
        timestamp=_TS,
        reason="close",
        strategy_id=MODEL_BLUE_STRATEGY_ID,
        action="CLOSE",
        trade_id="MBG-1",
    )
    assert persist_signal_id_for(signal) == "MBG-1:CLOSE"


@pytest.mark.asyncio
async def test_inbound_upsert_overwrites_empty_stub(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    trade_id = f"MBG-AUDIT-{uuid4().hex[:8]}"
    payload = {**SIL_GDX_PAYLOAD, "trade_id": trade_id}
    capture = {
        "metadata": {"request_id": "req-1"},
        "raw_body": str(payload),
        "parsed_json": payload,
    }
    signal = parse_tradingview_payload(
        payload, timestamp=_TS, request_id="req-1", capture_data=capture
    )

    async with db_factory() as session, session.begin():
        session.add(
            SignalModel(
                strategy_id=MODEL_BLUE_STRATEGY_ID,
                signal_id=trade_id,
                trade_id=trade_id,
                action="OPEN",
                pair="",
                side="N/A",
                ref_price_a=Decimal(0),
                raw_payload={},
                status="NEW",
            )
        )

    async with db_factory() as session, session.begin():
        row = await SignalRepository(session).record_inbound(signal)
        assert row.pair == "SIL:GDX"
        assert row.side == "1"
        assert row.ref_price_a == Decimal("90.64")
        assert row.ref_price_b == Decimal("91.86")
        assert row.raw_payload != {}
        assert row.raw_payload["parsed_json"]["trade_id"] == trade_id
        assert row.raw_payload["parsed_json"]["buckets"][1]["underlying"] == "GDX"
        assert row.status == SIGNAL_STATUS_NEW


@pytest.mark.asyncio
async def test_processed_status_not_downgraded_to_new(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    trade_id = f"MBG-AUDIT-{uuid4().hex[:8]}"
    payload = {**SIL_GDX_PAYLOAD, "trade_id": trade_id}
    capture = {"parsed_json": payload, "raw_body": "{}", "metadata": {}}
    signal = parse_tradingview_payload(
        payload, timestamp=_TS, request_id="req-2", capture_data=capture
    )
    async with db_factory() as session, session.begin():
        await SignalRepository(session).record_processed(signal, persist_signal_id=trade_id)

    async with db_factory() as session, session.begin():
        row = await SignalRepository(session).record_inbound(
            signal, persist_signal_id=trade_id, status=SIGNAL_STATUS_NEW
        )
        assert row.status == SIGNAL_STATUS_PROCESSED
        assert row.raw_payload["parsed_json"]["trade_id"] == trade_id


@pytest.mark.asyncio
async def test_rejected_parse_stores_original_json(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    trade_id = f"MBG-BAD-{uuid4().hex[:8]}"
    payload = {
        "strategy": "model_blue",
        "action": "OPEN",
        "trade_id": trade_id,
        "buckets": [],
    }
    capture = {
        "metadata": {"request_id": "bad-1"},
        "raw_body": '{"strategy":"model_blue"}',
        "parsed_json": payload,
    }
    async with db_factory() as session, session.begin():
        row = await SignalRepository(session).record_rejected_payload(
            payload,
            capture_data=capture,
            reason="MODEL_BLUE_INVALID_LEG_COUNT",
        )
        assert row.status == "REJECTED"
        assert row.raw_payload["parsed_json"]["trade_id"] == trade_id
        assert row.raw_payload != {}
        assert row.reject_reason and "LEG_COUNT" in row.reject_reason


@pytest.mark.asyncio
async def test_record_processed_fills_stub_then_keeps_capture(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    trade_id = f"MBG-AUDIT-{uuid4().hex[:8]}"
    payload = {**SIL_GDX_PAYLOAD, "trade_id": trade_id}
    capture = {"parsed_json": payload, "raw_body": "orig", "metadata": {"request_id": "x"}}
    signal = parse_tradingview_payload(
        payload, timestamp=_TS, request_id="x", capture_data=capture
    )
    async with db_factory() as session, session.begin():
        session.add(
            SignalModel(
                strategy_id=MODEL_BLUE_STRATEGY_ID,
                signal_id=trade_id,
                trade_id=trade_id,
                action="OPEN",
                pair="",
                side="N/A",
                ref_price_a=Decimal(0),
                raw_payload={},
                status="NEW",
            )
        )
    async with db_factory() as session, session.begin():
        row = await SignalRepository(session).record_processed(
            signal, persist_signal_id=trade_id
        )
        assert row.pair == "SIL:GDX"
        assert row.side == "1"
        assert row.raw_payload["parsed_json"] == payload
        assert row.status == SIGNAL_STATUS_PROCESSED
