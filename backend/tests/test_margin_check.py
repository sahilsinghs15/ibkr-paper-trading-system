"""RMS check 1 — MarginCheck."""

from datetime import UTC, datetime
from decimal import Decimal

from app.db.repositories.order_repository import _margin_impact_from_order
from app.oms.models import OMSOrder
from app.rms.checks.margin import MarginCheck
from app.rms.margin_estimate import SOURCE_DEFAULT, SOURCE_WHAT_IF
from app.rms.models import (
    ExecutionIntentMode,
    MarginPolicy,
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
    RMSContext,
    RMSOutcome,
)
from app.rms.models import OrderSide as RMSOrderSide
from app.services.account_margin import AccountMarginSnapshot


def _snap(**kwargs) -> AccountMarginSnapshot:
    fields = {
        "ibkr_account": "DU1",
        "currency": "USD",
        "as_of": datetime.now(UTC),
        "net_liquidation": Decimal(100000),
        "available_funds": Decimal(10000),
        "excess_liquidity": Decimal(12000),
        "max_age_sec": 300,
    }
    fields.update(kwargs)
    return AccountMarginSnapshot(**fields)


def _intent(*, account: str = "DU1", qty: float = 10, price: Decimal = Decimal(100), **kwargs) -> OrderIntent:
    return OrderIntent(
        signal_id=kwargs.pop("signal_id", "S1"),
        strategy_id="MODEL_BLUE",
        action=kwargs.pop("action", OrderAction.OPEN),
        ibkr_account=account,
        legs=kwargs.pop(
            "legs",
            [
                OrderLeg(
                    symbol="AAPL",
                    side=OrderSide.BUY,
                    quantity=qty,
                    price=price,
                    contract_month="2026-09",
                    instrument_type="STK",
                )
            ],
        ),
        **kwargs,
    )


def _enabled_policy(**kwargs) -> MarginPolicy:
    fields = {
        "check_enabled": True,
        "min_free_buffer": Decimal(0),
        "min_free_pct_of_netliq": Decimal(0),
        "comfort_ratio": Decimal("0.80"),
        "default_rate": Decimal("0.50"),
        "rate_safety_multiplier": Decimal("1.00"),
        "reject_on_stale_snapshot": True,
    }
    fields.update(kwargs)
    return MarginPolicy(**fields)


def test_insufficient_rejects_with_per_leg_sources() -> None:
    context = RMSContext(
        margin_policy=_enabled_policy(),
        margin_snapshots={"DU1": _snap(available_funds=Decimal(100))},
        margin_rates={("AAPL", "STK", "BUY"): Decimal("0.50")},
        margin_rate_sources={("AAPL", "STK", "BUY"): SOURCE_WHAT_IF},
    )
    # notional 1000 * 0.50 = 500 > 100
    result = MarginCheck().evaluate(_intent(qty=10, price=Decimal(100)), context)
    assert result.outcome == RMSOutcome.REJECT
    assert result.reason is not None
    assert "MARGIN_INSUFFICIENT" in result.reason
    assert "AAPL=WHAT_IF" in result.reason


def test_comfortable_and_borderline_pass_record_band() -> None:
    policy = _enabled_policy(default_rate=Decimal("0.10"))
    context = RMSContext(
        margin_policy=policy,
        margin_snapshots={"DU1": _snap(available_funds=Decimal(1000))},
    )
    comfortable = MarginCheck().evaluate(_intent(qty=1, price=Decimal(100)), context)
    assert comfortable.outcome == RMSOutcome.PASS
    assert comfortable.reason is not None
    assert "MARGIN_COMFORTABLE" in comfortable.reason

    # required = 1000 * 0.10 = 100; usable=120 so 100 is borderline (comfort=96)
    borderline_ctx = RMSContext(
        margin_policy=policy,
        margin_snapshots={"DU1": _snap(available_funds=Decimal(120))},
    )
    borderline = MarginCheck().evaluate(_intent(qty=10, price=Decimal(100)), borderline_ctx)
    assert borderline.outcome == RMSOutcome.PASS
    assert borderline.reason is not None
    assert "MARGIN_BORDERLINE" in borderline.reason


def test_missing_snapshot_rejects() -> None:
    context = RMSContext(margin_policy=_enabled_policy())
    result = MarginCheck().evaluate(_intent(), context)
    assert result.outcome == RMSOutcome.REJECT
    assert result.reason is not None
    assert "MARGIN_SNAPSHOT_UNAVAILABLE" in result.reason


def test_disabled_shadow_mode_passes() -> None:
    context = RMSContext(margin_policy=MarginPolicy(check_enabled=False))
    result = MarginCheck().evaluate(_intent(), context)
    assert result.outcome == RMSOutcome.PASS
    assert result.reason is not None
    assert "MARGIN_CHECK_DISABLED" in result.reason


def test_two_legs_sum_never_net() -> None:
    policy = _enabled_policy(default_rate=Decimal("0.50"))
    context = RMSContext(
        margin_policy=policy,
        margin_snapshots={"DU1": _snap(available_funds=Decimal(900))},
    )
    intent = _intent(
        legs=[
            OrderLeg(
                symbol="AAPL",
                side=OrderSide.BUY,
                quantity=10,
                price=Decimal(100),
                contract_month="2026-09",
            ),
            OrderLeg(
                symbol="MSFT",
                side=OrderSide.SELL,
                quantity=10,
                price=Decimal(100),
                contract_month="2026-09",
            ),
        ]
    )
    # 500 + 500 = 1000 >= 900 → INSUFFICIENT (netting would be 0)
    result = MarginCheck().evaluate(intent, context)
    assert result.outcome == RMSOutcome.REJECT
    assert result.reason is not None
    assert "MARGIN_INSUFFICIENT" in result.reason


def test_close_and_flatten_pass_at_zero_headroom() -> None:
    context = RMSContext(
        margin_policy=_enabled_policy(),
        margin_snapshots={"DU1": _snap(available_funds=Decimal(0))},
    )
    close = MarginCheck().evaluate(_intent(action=OrderAction.CLOSE), context)
    flatten = MarginCheck().evaluate(
        _intent(action=OrderAction.CLOSE, intent_mode=ExecutionIntentMode.EMERGENCY_FLATTEN),
        context,
    )
    assert close.outcome == RMSOutcome.PASS
    assert flatten.outcome == RMSOutcome.PASS


def test_margin_impact_only_when_broker_derived() -> None:
    what_if_intent = _intent(
        legs=[
            OrderLeg(
                symbol="AAPL",
                side=OrderSide.BUY,
                quantity=1,
                price=Decimal(100),
                contract_month="2026-09",
                metadata={"margin_impact": "12.5", "margin_source": SOURCE_WHAT_IF},
            )
        ]
    )
    default_intent = _intent(
        legs=[
            OrderLeg(
                symbol="AAPL",
                side=OrderSide.BUY,
                quantity=1,
                price=Decimal(100),
                contract_month="2026-09",
                metadata={"margin_impact": "12.5", "margin_source": SOURCE_DEFAULT},
            )
        ]
    )
    what_if_order = OMSOrder(
        internal_order_id="o1",
        intent=what_if_intent,
        symbol="AAPL",
        side=RMSOrderSide.BUY,
        quantity=1,
        leg_index=0,
    )
    default_order = OMSOrder(
        internal_order_id="o2",
        intent=default_intent,
        symbol="AAPL",
        side=RMSOrderSide.BUY,
        quantity=1,
        leg_index=0,
    )
    assert _margin_impact_from_order(what_if_order) == Decimal("12.5")
    assert _margin_impact_from_order(default_order) is None
