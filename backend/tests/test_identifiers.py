"""Identifier canon: symbols and accounts fold; trade/signal ids do not."""

from decimal import Decimal

from app.core.identifiers import (
    normalize_account,
    normalize_signal_id,
    normalize_strategy_id,
    normalize_symbol,
)
from app.rms.checks.duplicate import DuplicateCheck
from app.rms.checks.money_per_stock import MoneyPerStockCheck
from app.rms.models import (
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
    RMSContext,
    RMSOutcome,
    duplicate_lookup_key,
    exposure_key,
)


def test_normalize_symbol_strips_and_uppercases() -> None:
    assert normalize_symbol(" aapl ") == "AAPL"
    assert normalize_symbol("AAPL") == "AAPL"


def test_normalize_account_strips_and_uppercases() -> None:
    assert normalize_account(" dua12345 ") == "DUA12345"


def test_normalize_signal_id_preserves_case() -> None:
    assert normalize_signal_id("  MbG-Trade-1  ") == "MbG-Trade-1"


def test_duplicate_lookup_key_folds_strategy_id() -> None:
    intent = OrderIntent(
        signal_id="SIG_001",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(
                symbol="AAPL",
                side=OrderSide.BUY,
                quantity=10,
                price=Decimal(150),
                contract_month="",
            )
        ],
    )
    assert duplicate_lookup_key(intent) == ("model_blue", "SIG_001")
    intent_acct = OrderIntent(
        signal_id="SIG_001",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        account_id=7,
        legs=intent.legs,
    )
    assert duplicate_lookup_key(intent_acct) == (7, "model_blue", "SIG_001")


def test_duplicate_check_folds_strategy_id_casing() -> None:
    check = DuplicateCheck()
    context = RMSContext(processed_signals={("model_blue", "SIG_001")})
    intent = OrderIntent(
        signal_id="SIG_001",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        legs=[
            OrderLeg(
                symbol="AAPL",
                side=OrderSide.BUY,
                quantity=10,
                price=Decimal(150),
                contract_month="",
            )
        ],
    )
    result = check.evaluate(intent, context)
    assert result.outcome == RMSOutcome.REJECT
    assert "DUPLICATE_SIGNAL" in (result.reason or "")


def test_lowercase_inbound_symbol_hits_same_exposure_bucket() -> None:
    intent = OrderIntent(
        signal_id="SIG_A",
        strategy_id="model_blue",
        action=OrderAction.OPEN,
        account_id=1,
        legs=[
            OrderLeg(
                symbol="aapl",
                side=OrderSide.BUY,
                quantity=10,
                price=Decimal(100),
                contract_month="",
            )
        ],
    )
    assert exposure_key(intent, "aapl") == (1, "AAPL")
    assert exposure_key(intent, "AAPL") == (1, "AAPL")

    check = MoneyPerStockCheck()
    context = RMSContext(
        default_symbol_limits={},
        per_symbol_limits={(1, "AAPL"): Decimal("500")},
        symbol_exposures={(1, "AAPL"): Decimal("400")},
    )
    result = check.evaluate(intent, context)
    assert result.outcome == RMSOutcome.REJECT
    assert "MONEY_LIMIT_EXCEEDED" in (result.reason or "")


def test_normalize_strategy_id_used_by_duplicate_key() -> None:
    assert normalize_strategy_id("MODEL_BLUE") == "model_blue"
