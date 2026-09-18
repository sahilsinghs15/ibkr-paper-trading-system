"""CHECK 8 — per-symbol limit basis depends on the account's Cancel Exposure setting.

OFF (default): gross basis — every leg adds its notional, regardless of side.
ON: net basis — net shares after the trade (as the broker nets them), valued at
the order's price, must stay within the limit; net-reducing trades always pass.
"""

from decimal import Decimal

import pytest

from app.rms.checks.money_per_stock import MoneyPerStockCheck
from app.rms.models import (
    ExecutionIntentMode,
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
    RMSContext,
    RMSOutcome,
    StrategyConfig,
)

ACC = 7
LIMIT = Decimal(25000)


def _context(*, cancel_exposure: bool, gross: Decimal, net: Decimal) -> RMSContext:
    """Existing AAPL position: gross notional and net value (at $200/share) as given.

    ``net`` is expressed in dollars at $200 for readability and stored as shares.
    AAPL limit $25k.
    """
    return RMSContext(
        strategy_configs={
            "model_blue": StrategyConfig(
                strategy_id="model_blue", max_open_positions=10, money_limit_per_symbol=Decimal(0)
            )
        },
        per_symbol_limits={(ACC, "AAPL"): LIMIT},
        default_symbol_limits={ACC: Decimal(1_000_000)},
        symbol_exposures={(ACC, "AAPL"): gross},
        symbol_net_quantities={(ACC, "AAPL"): net / Decimal(200)},
        cancel_exposure_accounts={ACC} if cancel_exposure else set(),
    )


def _intent(side: OrderSide, notional: int, *, other: str = "XYZ") -> OrderIntent:
    other_side = OrderSide.BUY if side == OrderSide.SELL else OrderSide.SELL
    return OrderIntent(
        signal_id="SIG",
        strategy_id="model_blue",
        action=OrderAction.OPEN,
        account_id=ACC,
        legs=[
            OrderLeg(symbol="AAPL", side=side, quantity=notional / 200, price=Decimal(200)),
            OrderLeg(symbol=other, side=other_side, quantity=10, price=Decimal(10)),
        ],
    )


def _eval(ctx: RMSContext, intent: OrderIntent):
    return MoneyPerStockCheck().evaluate(intent, ctx)


# ----------------------------------------------------------------- OFF: gross (unchanged)


def test_off_gross_basis_counts_opposite_side_as_added_exposure():
    """OFF keeps today's behaviour: a SELL against a $20k long is counted as +$10k."""
    ctx = _context(cancel_exposure=False, gross=Decimal(20000), net=Decimal(20000))
    res = _eval(ctx, _intent(OrderSide.SELL, 10000))
    assert res.outcome == RMSOutcome.REJECT
    assert "total exposure of 30000" in (res.reason or "")


def test_off_still_enforces_limit_when_adding():
    ctx = _context(cancel_exposure=False, gross=Decimal(20000), net=Decimal(20000))
    res = _eval(ctx, _intent(OrderSide.BUY, 10000))
    assert res.outcome == RMSOutcome.REJECT


def test_off_passes_within_limit():
    ctx = _context(cancel_exposure=False, gross=Decimal(10000), net=Decimal(10000))
    assert _eval(ctx, _intent(OrderSide.BUY, 10000)).outcome == RMSOutcome.PASS


# ----------------------------------------------------------------- ON: net basis


def test_on_net_reducing_trade_passes():
    """Long $20k, SELL $10k -> net $10k: allowed (gross basis would have rejected)."""
    ctx = _context(cancel_exposure=True, gross=Decimal(20000), net=Decimal(20000))
    assert _eval(ctx, _intent(OrderSide.SELL, 10000)).outcome == RMSOutcome.PASS


def test_on_adding_beyond_limit_is_rejected_on_net_basis():
    ctx = _context(cancel_exposure=True, gross=Decimal(20000), net=Decimal(20000))
    res = _eval(ctx, _intent(OrderSide.BUY, 10000))
    assert res.outcome == RMSOutcome.REJECT
    assert "net exposure of 30000" in (res.reason or "")
    assert "net qty 100 -> 150" in (res.reason or "")
    assert "net basis" in (res.reason or "")


def test_on_adding_up_to_limit_passes():
    ctx = _context(cancel_exposure=True, gross=Decimal(20000), net=Decimal(20000))
    assert _eval(ctx, _intent(OrderSide.BUY, 5000)).outcome == RMSOutcome.PASS


@pytest.mark.parametrize(
    ("sell", "expected"),
    [
        (40000, RMSOutcome.PASS),  # +20k -> -20k: flipped short within limit
        (45000, RMSOutcome.PASS),  # +20k -> -25k: exactly at limit
        (50000, RMSOutcome.REJECT),  # +20k -> -30k: flipped short beyond limit
    ],
)
def test_on_flip_is_checked_on_new_position_size(sell, expected):
    ctx = _context(cancel_exposure=True, gross=Decimal(20000), net=Decimal(20000))
    assert _eval(ctx, _intent(OrderSide.SELL, sell)).outcome == expected


def test_on_reducing_trade_passes_even_when_already_above_limit():
    """Limit lowered below the existing position: reducing it must not be blocked."""
    ctx = _context(cancel_exposure=True, gross=Decimal(40000), net=Decimal(40000))
    assert _eval(ctx, _intent(OrderSide.SELL, 5000)).outcome == RMSOutcome.PASS


def test_on_short_position_reduced_by_buy_passes():
    ctx = _context(cancel_exposure=True, gross=Decimal(24000), net=Decimal(-24000))
    assert _eval(ctx, _intent(OrderSide.BUY, 10000)).outcome == RMSOutcome.PASS


def test_on_hedged_book_uses_net_not_gross():
    """Offsetting legs across pairs: gross $40k but net $0 -> new $20k BUY passes."""
    ctx = _context(cancel_exposure=True, gross=Decimal(40000), net=Decimal(0))
    assert _eval(ctx, _intent(OrderSide.BUY, 20000)).outcome == RMSOutcome.PASS


def test_on_still_requires_a_configured_limit():
    ctx = _context(cancel_exposure=True, gross=Decimal(0), net=Decimal(0))
    ctx.per_symbol_limits.clear()
    ctx.default_symbol_limits.clear()
    res = _eval(ctx, _intent(OrderSide.BUY, 1000))
    assert res.outcome == RMSOutcome.REJECT
    assert "NO_SYMBOL_LIMIT_CONFIGURED" in (res.reason or "")


def test_on_setting_is_per_account():
    """Another account with Cancel Exposure ON does not switch this account to net."""
    ctx = _context(cancel_exposure=False, gross=Decimal(20000), net=Decimal(20000))
    ctx.cancel_exposure_accounts.add(ACC + 1)
    assert _eval(ctx, _intent(OrderSide.SELL, 10000)).outcome == RMSOutcome.REJECT


@pytest.mark.parametrize("cancel_exposure", [False, True])
def test_close_and_emergency_flatten_are_never_blocked(cancel_exposure):
    ctx = _context(cancel_exposure=cancel_exposure, gross=Decimal(900000), net=Decimal(900000))
    close = _intent(OrderSide.BUY, 500000)
    close = OrderIntent(**{**close.__dict__, "action": OrderAction.CLOSE})
    flatten = OrderIntent(**{**close.__dict__, "action": OrderAction.OPEN,
                             "intent_mode": ExecutionIntentMode.EMERGENCY_FLATTEN})
    assert _eval(ctx, close).outcome == RMSOutcome.PASS
    assert _eval(ctx, flatten).outcome == RMSOutcome.PASS


def test_on_values_net_shares_at_order_price_not_historic_entry_prices():
    """Flat in shares (bought 100 @ $200, sold 100 @ $250) is $0 net exposure.

    Gross is $45k; a new $20k BUY at $200 leaves 100 shares = $20k <= $25k limit.
    """
    ctx = _context(cancel_exposure=True, gross=Decimal(45000), net=Decimal(0))
    assert _eval(ctx, _intent(OrderSide.BUY, 20000)).outcome == RMSOutcome.PASS


def test_on_net_value_uses_current_order_price():
    """100 shares held (bought cheaply). At today's $300 order price, +10 shares =
    110 * $300 = $33k > $25k -> rejected even though entry-price value was $20k."""
    ctx = _context(cancel_exposure=True, gross=Decimal(20000), net=Decimal(20000))
    intent = OrderIntent(
        signal_id="SIG",
        strategy_id="model_blue",
        action=OrderAction.OPEN,
        account_id=ACC,
        legs=[
            OrderLeg(symbol="AAPL", side=OrderSide.BUY, quantity=10, price=Decimal(300)),
            OrderLeg(symbol="XYZ", side=OrderSide.SELL, quantity=10, price=Decimal(10)),
        ],
    )
    res = _eval(ctx, intent)
    assert res.outcome == RMSOutcome.REJECT
    assert "net exposure of 33000" in (res.reason or "")
