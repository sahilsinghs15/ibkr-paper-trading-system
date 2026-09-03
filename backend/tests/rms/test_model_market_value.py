"""Unit tests for RMS check 101 — MODEL MARKET VALUE."""

from decimal import Decimal

from app.rms.checks.model_market_value import ModelMarketValueCheck
from app.rms.models import (
    ExecutionIntentMode,
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
    RMSContext,
    RMSOutcome,
)

_CEILING = Decimal(500)
_PAIR = Decimal(50)


def _intent(
    *,
    account_id: int | None = 1,
    strategy_id: str = "model_blue",
    action: OrderAction = OrderAction.OPEN,
    value: Decimal = _PAIR,
    intent_mode: ExecutionIntentMode = ExecutionIntentMode.OPEN,
) -> OrderIntent:
    half = value / Decimal(2)
    return OrderIntent(
        signal_id="T-101",
        strategy_id=strategy_id,
        action=action,
        account_id=account_id,
        intent_mode=intent_mode,
        legs=[
            OrderLeg(
                symbol="XLE",
                side=OrderSide.BUY,
                quantity=1,
                price=half,
                contract_month="2026-09",
                notional=half,
            ),
            OrderLeg(
                symbol="XOP",
                side=OrderSide.SELL,
                quantity=1,
                price=half,
                contract_month="2026-09",
                notional=half,
            ),
        ],
    )


def _ctx(
    *,
    used: Decimal = Decimal(0),
    limit: Decimal | None = _CEILING,
    enabled: bool = True,
    extra_limits: dict | None = None,
    extra_used: dict | None = None,
) -> RMSContext:
    limits: dict[tuple[int, str], Decimal] = {}
    used_map: dict[tuple[int, str], Decimal] = {}
    if limit is not None:
        limits[(1, "model_blue")] = limit
    used_map[(1, "model_blue")] = used
    if extra_limits:
        limits.update(extra_limits)
    if extra_used:
        used_map.update(extra_used)
    return RMSContext(
        model_value_limit=limits,
        model_value_used=used_map,
        market_value_check_enabled=enabled,
    )


def test_pairs_one_through_ten_pass_eleventh_rejects() -> None:
    check = ModelMarketValueCheck()
    for n in range(10):
        result = check.evaluate(_intent(), _ctx(used=_PAIR * n))
        assert result.outcome == RMSOutcome.PASS, n
    eleventh = check.evaluate(_intent(), _ctx(used=_PAIR * 10))
    assert eleventh.outcome == RMSOutcome.REJECT
    assert "MODEL_VALUE_EXCEEDED" in (eleventh.reason or "")


def test_exactly_at_ceiling_passes() -> None:
    result = ModelMarketValueCheck().evaluate(_intent(), _ctx(used=Decimal(450)))
    assert result.outcome == RMSOutcome.PASS


def test_one_dollar_over_rejects() -> None:
    result = ModelMarketValueCheck().evaluate(_intent(), _ctx(used=Decimal(451)))
    assert result.outcome == RMSOutcome.REJECT
    assert "MODEL_VALUE_EXCEEDED" in (result.reason or "")


def test_close_over_ceiling_passes() -> None:
    result = ModelMarketValueCheck().evaluate(
        _intent(action=OrderAction.CLOSE),
        _ctx(used=_CEILING),
    )
    assert result.outcome == RMSOutcome.PASS


def test_emergency_flatten_over_ceiling_passes() -> None:
    result = ModelMarketValueCheck().evaluate(
        _intent(intent_mode=ExecutionIntentMode.EMERGENCY_FLATTEN),
        _ctx(used=_CEILING),
    )
    assert result.outcome == RMSOutcome.PASS


def test_missing_limit_rejects() -> None:
    result = ModelMarketValueCheck().evaluate(_intent(), _ctx(limit=None))
    assert result.outcome == RMSOutcome.REJECT
    assert "MODEL_VALUE_LIMIT_UNKNOWN" in (result.reason or "")


def test_shadow_mode_passes() -> None:
    result = ModelMarketValueCheck().evaluate(_intent(), _ctx(enabled=False, limit=None))
    assert result.outcome == RMSOutcome.PASS
    assert "MARKET_VALUE_CHECK_DISABLED" in (result.reason or "")


def test_no_account_scope_passes() -> None:
    result = ModelMarketValueCheck().evaluate(_intent(account_id=None), _ctx())
    assert result.outcome == RMSOutcome.PASS
    assert "NO_ACCOUNT_SCOPE" in (result.reason or "")


def test_two_models_on_one_account_have_independent_ceilings() -> None:
    ctx = _ctx(
        used=_CEILING,
        extra_limits={(1, "model_white"): _CEILING},
        extra_used={(1, "model_white"): Decimal(0)},
    )
    check = ModelMarketValueCheck()
    blue = check.evaluate(_intent(), ctx)
    white = check.evaluate(_intent(strategy_id="model_white"), ctx)
    assert blue.outcome == RMSOutcome.REJECT
    assert white.outcome == RMSOutcome.PASS


def test_same_model_on_two_accounts_has_independent_ceilings() -> None:
    ctx = _ctx(
        used=_CEILING,
        extra_limits={(2, "model_blue"): _CEILING},
        extra_used={(2, "model_blue"): Decimal(0)},
    )
    check = ModelMarketValueCheck()
    a = check.evaluate(_intent(account_id=1), ctx)
    b = check.evaluate(_intent(account_id=2), ctx)
    assert a.outcome == RMSOutcome.REJECT
    assert b.outcome == RMSOutcome.PASS


def test_round_down_slack_leaves_room_for_eleventh() -> None:
    """Ten realised $45 pairs leave room for an eleventh; ten $50 budgets would not."""
    realised = Decimal(45)
    result = ModelMarketValueCheck().evaluate(
        _intent(value=realised),
        _ctx(used=realised * 10),
    )
    assert result.outcome == RMSOutcome.PASS
    assert realised * 10 + realised <= _CEILING
