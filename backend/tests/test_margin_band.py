"""Three-tier band classifier and directional rate lookup."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.margin_rate import MarginRateModel
from app.rms.margin_estimate import (
    SOURCE_DEFAULT,
    SOURCE_WHAT_IF,
    MarginBand,
    classify_headroom,
    estimate_required_margin,
    rate_for,
)
from app.rms.models import (
    MarginPolicy,
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
    RMSContext,
)
from app.services.margin_rate import MarginRateService


def _policy(**kwargs) -> MarginPolicy:
    defaults = {
        "check_enabled": True,
        "min_free_buffer": Decimal(0),
        "min_free_pct_of_netliq": Decimal(0),
        "comfort_ratio": Decimal("0.80"),
        "default_rate": Decimal("0.30"),
        "rate_safety_multiplier": Decimal("1.10"),
    }
    defaults.update(kwargs)
    return MarginPolicy(**defaults)


def test_classify_edges_at_comfort_ratio_and_usable() -> None:
    policy = _policy()
    usable = Decimal(1000)
    comfort = usable * policy.comfort_ratio
    assert classify_headroom(comfort - Decimal("0.01"), effective_free=usable, policy=policy) is MarginBand.COMFORTABLE
    assert classify_headroom(comfort, effective_free=usable, policy=policy) is MarginBand.BORDERLINE
    assert classify_headroom(usable - Decimal("0.01"), effective_free=usable, policy=policy) is MarginBand.BORDERLINE
    assert classify_headroom(usable, effective_free=usable, policy=policy) is MarginBand.INSUFFICIENT


def test_short_leg_uses_sell_rate_not_buy() -> None:
    policy = _policy()
    rates = {
        ("XOP", "STK", "BUY"): Decimal("0.50"),
        ("XOP", "STK", "SELL"): Decimal("0.20"),
    }
    sources = {
        ("XOP", "STK", "BUY"): SOURCE_DEFAULT,
        ("XOP", "STK", "SELL"): SOURCE_DEFAULT,
    }
    intent = OrderIntent(
        signal_id="p",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(
                symbol="XOP",
                side=OrderSide.SELL,
                quantity=10,
                price=Decimal(100),
                contract_month="2026-09",
                instrument_type="STK",
            )
        ],
    )
    required, per_leg = estimate_required_margin(intent, rates, sources, policy)
    assert required == Decimal(200)
    assert per_leg["XOP"] == SOURCE_DEFAULT


def test_unknown_symbol_uses_default_rate() -> None:
    policy = _policy(default_rate=Decimal("0.30"))
    rate, source = rate_for("ZZZ", "CFD", OrderSide.BUY, {}, {}, policy)
    assert rate == Decimal("0.30")
    assert source == SOURCE_DEFAULT


def test_safety_multiplier_applies_only_to_what_if() -> None:
    policy = _policy(rate_safety_multiplier=Decimal("1.10"))
    rates = {
        ("AAPL", "STK", "BUY"): Decimal("0.20"),
        ("MSFT", "STK", "BUY"): Decimal("0.20"),
    }
    sources = {
        ("AAPL", "STK", "BUY"): SOURCE_WHAT_IF,
        ("MSFT", "STK", "BUY"): SOURCE_DEFAULT,
    }
    wi, src_wi = rate_for("AAPL", "STK", OrderSide.BUY, rates, sources, policy)
    de, src_de = rate_for("MSFT", "STK", OrderSide.BUY, rates, sources, policy)
    assert src_wi == SOURCE_WHAT_IF
    assert wi == Decimal("0.22")
    assert src_de == SOURCE_DEFAULT
    assert de == Decimal("0.20")


@pytest.mark.asyncio
async def test_stale_rate_row_is_ignored(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    svc = MarginRateService(session_factory)
    stale_symbol = f"STALE{uuid4().hex[:8]}".upper()
    fresh_symbol = f"FRESH{uuid4().hex[:8]}".upper()
    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        session.add(
            MarginRateModel(
                symbol=stale_symbol,
                instrument_type="STK",
                side="BUY",
                rate=Decimal("0.11"),
                source=SOURCE_WHAT_IF,
                probe_notional=Decimal(1000),
                init_margin_change=Decimal(110),
                scanned_at=now - timedelta(days=30),
                updated_at=now - timedelta(days=30),
            )
        )
        session.add(
            MarginRateModel(
                symbol=fresh_symbol,
                instrument_type="STK",
                side="BUY",
                rate=Decimal("0.22"),
                source=SOURCE_WHAT_IF,
                probe_notional=Decimal(1000),
                init_margin_change=Decimal(220),
                scanned_at=now,
                updated_at=now,
            )
        )
    context = RMSContext()
    await svc.load_into(context, _policy())
    assert (stale_symbol, "STK", "BUY") not in context.margin_rates
    assert (fresh_symbol, "STK", "BUY") in context.margin_rates
