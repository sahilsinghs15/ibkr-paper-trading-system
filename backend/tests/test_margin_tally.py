"""Running committed-margin tally bridging the accountSummary refresh window."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.oms.models import OMSOrder
from app.rms.checks.margin import MarginCheck
from app.rms.margin_estimate import COMMITMENT_GRACE, pending_commitments
from app.rms.models import (
    MarginPolicy,
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
    RMSOutcome,
)
from app.rms.models import OrderSide as RMSOrderSide
from app.services.account_margin import AccountMarginSnapshot
from app.services.order_manager import OrderManager


def _policy() -> MarginPolicy:
    return MarginPolicy(
        check_enabled=True,
        min_free_buffer=Decimal(0),
        min_free_pct_of_netliq=Decimal(0),
        comfort_ratio=Decimal("0.80"),
        default_rate=Decimal(1),
        rate_safety_multiplier=Decimal(1),
        confirm_borderline=False,
    )


def _snap(available: Decimal, as_of: datetime | None = None) -> AccountMarginSnapshot:
    return AccountMarginSnapshot(
        ibkr_account="DU1",
        as_of=as_of or datetime.now(UTC),
        net_liquidation=Decimal(100000),
        available_funds=available,
        max_age_sec=300,
    )


def _open_intent(signal_id: str, notional: Decimal = Decimal(100)) -> OrderIntent:
    qty = float(notional)
    return OrderIntent(
        signal_id=signal_id,
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        ibkr_account="DU1",
        legs=[
            OrderLeg(
                symbol="AAPL",
                side=OrderSide.BUY,
                quantity=qty,
                price=Decimal(1),
                contract_month="2026-09",
                notional=notional,
            )
        ],
    )


def test_ten_opens_in_one_snapshot_window_consume_headroom() -> None:
    mgr = OrderManager(oms=None)
    mgr._rms_context.margin_policy = _policy()
    mgr._rms_context.margin_snapshots["DU1"] = _snap(Decimal(550))
    check = MarginCheck()
    outcomes: list[RMSOutcome] = []
    for i in range(10):
        intent = _open_intent(f"S{i}")
        result = check.evaluate(intent, mgr._rms_context)
        outcomes.append(result.outcome)
        if result.outcome == RMSOutcome.PASS:
            mgr._commit_margin(intent, opening=True)
    # Each OPEN requires 100; 550 headroom fits five. Without the tally all ten would PASS.
    assert outcomes.count(RMSOutcome.PASS) == 5
    assert outcomes.count(RMSOutcome.REJECT) == 5
    assert all(o is RMSOutcome.REJECT for o in outcomes[5:])


def test_fresh_snapshot_keeps_boundary_and_newer_commitments() -> None:
    as_of = datetime.now(UTC)
    grace = COMMITMENT_GRACE
    boundary = as_of - grace
    older = as_of - grace - timedelta(seconds=1)
    newer = as_of - grace + timedelta(seconds=1)
    commitments = [
        (older, Decimal(10)),
        (boundary, Decimal(20)),
        (newer, Decimal(30)),
    ]
    kept = pending_commitments(commitments, as_of)
    assert kept == Decimal(50)

    mgr = OrderManager(oms=None)
    mgr._rms_context.margin_commitments["DU1"] = list(commitments)
    mgr._on_margin_snapshot(_snap(Decimal(1000), as_of=as_of))
    remaining = mgr._rms_context.margin_commitments["DU1"]
    amounts = {amount for _ts, amount in remaining}
    assert Decimal(10) not in amounts
    assert Decimal(20) in amounts
    assert Decimal(30) in amounts


def test_close_appends_negative_commitment() -> None:
    mgr = OrderManager(oms=None)
    mgr._rms_context.margin_policy = _policy()
    intent = _open_intent("C1")
    mgr._commit_margin(intent, opening=True)
    mgr._commit_margin(intent, opening=False)
    amounts = [amount for _ts, amount in mgr._rms_context.margin_commitments["DU1"]]
    assert amounts == [Decimal(100), Decimal(-100)]


def test_unsettled_partial_fill_books_fill_notional() -> None:
    mgr = OrderManager(oms=None)
    mgr._rms_context.margin_policy = _policy()
    intent = _open_intent("U1", notional=Decimal(100))
    order = OMSOrder(
        internal_order_id="ord-u1",
        intent=intent,
        symbol="AAPL",
        side=RMSOrderSide.BUY,
        quantity=100,
        filled_quantity=40,
        leg_index=0,
        average_fill_price=Decimal(1),
    )
    mgr._record_unsettled_exposure(intent, [order])
    amounts = [amount for _ts, amount in mgr._rms_context.margin_commitments.get("DU1", [])]
    assert amounts == [Decimal(40)]
    assert mgr._rms_context.symbol_exposures["AAPL"] == Decimal(40)
