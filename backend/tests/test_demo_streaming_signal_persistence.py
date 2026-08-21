"""Unit tests for demo_streaming load_signals account resolution and RMS-rejected signal persistence."""

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.account import AccountModel
from app.db.models.order import OrderModel
from app.db.models.signal import SignalJobModel, SignalModel
from demo_streaming.snapshot import load_signals


@pytest.fixture
async def session_factory():
    from app.db.session import AsyncSessionLocal, engine
    yield AsyncSessionLocal
    await engine.dispose()


@pytest.mark.asyncio
async def test_load_signals_includes_account_metadata_and_rejected_signals(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Verify load_signals returns account_id, ibkr_account and includes RMS-rejected signals without orders."""
    test_id = uuid4().hex[:6]
    ibkr_acc = f"DU{test_id}"
    sig_id_accepted = f"SIG-ACC-{test_id}"
    sig_id_rejected = f"SIG-REJ-{test_id}"

    async with session_factory() as session, session.begin():
        acc = AccountModel(name="TestAccount", ibkr_account=ibkr_acc, total_margin=Decimal("100000.00"))
        session.add(acc)
        await session.flush()
        acc_id = acc.id

        # 1. Signal with Order (Accepted)
        sig1 = SignalModel(
            signal_id=sig_id_accepted,
            strategy_id="model_blue",
            action="OPEN",
            pair="EWA / EZU",
            side="BUY",
            ref_price_a=Decimal("30.0"),
            ref_price_b=Decimal("70.0"),
            status="PROCESSED",
            raw_payload={"account": ibkr_acc},
        )
        session.add(sig1)
        await session.flush()

        order1 = OrderModel(
            signal_id=sig1.id,
            account_id=acc_id,
            strategy_id="model_blue",
            leg="L0",
            symbol="EWA",
            ibkr_contract="STK",
            buy_sell="BUY",
            quantity=Decimal("100.0"),
            limit_price=Decimal("30.0"),
            status="FILLED",
            fill_qty=Decimal("100.0"),
        )
        session.add(order1)

        # 2. RMS-rejected signal (No orders created)
        sig2 = SignalModel(
            signal_id=sig_id_rejected,
            strategy_id="model_blue",
            action="OPEN",
            pair="AAPL / MSFT",
            side="BUY",
            ref_price_a=Decimal("150.0"),
            ref_price_b=Decimal("300.0"),
            status="REJECTED",
            reject_reason="RMS Risk Limit Exceeded",
            raw_payload={"account": ibkr_acc},
        )
        session.add(sig2)
        await session.flush()

        job2 = SignalJobModel(
            signal_id=sig_id_rejected,
            strategy_id="model_blue",
            status="REJECTED",
            idempotency_key=f"idem-{test_id}",
            account_scope=ibkr_acc,
            raw_payload={"account": ibkr_acc},
            correlation_id=f"corr-{test_id}",
        )
        session.add(job2)

    # Execute load_signals filtered by ibkr_account
    async with session_factory() as session:
        res = await load_signals(session, ibkr_account=ibkr_acc, return_dict=True)

    assert isinstance(res, dict)
    signals = res.get("signals", [])
    assert len(signals) >= 2

    # Verify both signals carry ibkr_account and account_id
    sig_ids = [s["signal_id"] for s in signals]
    assert sig_id_accepted in sig_ids
    assert sig_id_rejected in sig_ids

    for s in signals:
        if s["signal_id"] in (sig_id_accepted, sig_id_rejected):
            assert s["ibkr_account"] == ibkr_acc
            assert s["account_id"] == acc_id
