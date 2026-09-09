"""Pure unit tests for pair and account risk-exit evaluation."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.risk_exit_rules import (
    EXIT_UNIT_ABSOLUTE,
    EXIT_UNIT_PERCENT,
    REASON_ACCOUNT_STOP,
    REASON_ACCOUNT_TARGET,
    REASON_PAIR_STOP,
    REASON_PAIR_TARGET,
    REASON_PAIR_TIME_LIMIT,
    AccountRiskParams,
    PairExitParams,
    evaluate_account_risk,
    evaluate_pair_exit,
    pair_entry_gross_notional,
    resolve_threshold,
)

NOW = datetime(2026, 9, 9, 14, 0, tzinfo=UTC)
OPENED = NOW - timedelta(seconds=600)
NOTIONAL = Decimal(10000)


def _pair(**overrides) -> PairExitParams:
    base = {
        "target": Decimal(500),
        "stop": Decimal(250),
        "time_limit": 3600,
        "target_unit": EXIT_UNIT_ABSOLUTE,
        "stop_unit": EXIT_UNIT_ABSOLUTE,
        "opened_at": OPENED,
        "entry_gross_notional": NOTIONAL,
    }
    base.update(overrides)
    return PairExitParams(**base)


def _account(**overrides) -> AccountRiskParams:
    base = {
        "daily_target": Decimal(2000),
        "daily_stop": Decimal(1000),
        "daily_target_unit": EXIT_UNIT_ABSOLUTE,
        "daily_stop_unit": EXIT_UNIT_ABSOLUTE,
        "total_margin": Decimal(100000),
    }
    base.update(overrides)
    return AccountRiskParams(**base)


def test_pair_gross_notional_sums_abs_qty_times_entry() -> None:
    assert pair_entry_gross_notional(
        leg_a_signed_qty=Decimal(10),
        leg_a_entry_mark=Decimal(100),
        leg_b_signed_qty=Decimal(-5),
        leg_b_entry_mark=Decimal(200),
    ) == Decimal(2000)


def test_resolve_threshold_null_or_zero_disables() -> None:
    assert resolve_threshold(None, EXIT_UNIT_ABSOLUTE, basis=NOTIONAL) is None
    assert resolve_threshold(Decimal(0), EXIT_UNIT_ABSOLUTE, basis=NOTIONAL) is None
    assert resolve_threshold(Decimal(-1), EXIT_UNIT_ABSOLUTE, basis=NOTIONAL) is None


def test_resolve_threshold_percent_of_basis() -> None:
    assert resolve_threshold(
        Decimal("0.05"), EXIT_UNIT_PERCENT, basis=NOTIONAL
    ) == Decimal(500)
    assert resolve_threshold(Decimal("0.05"), EXIT_UNIT_PERCENT, basis=Decimal(0)) is None
    assert resolve_threshold(Decimal(5), "unknown", basis=NOTIONAL) is None


def test_pair_stop_absolute() -> None:
    decision = evaluate_pair_exit(_pair(), pnl=Decimal(-250), now=NOW)
    assert decision is not None
    assert decision.reason == REASON_PAIR_STOP
    assert decision.threshold == Decimal(250)


def test_pair_stop_not_yet() -> None:
    assert evaluate_pair_exit(_pair(), pnl=Decimal("-249.99"), now=NOW) is None


def test_pair_target_absolute() -> None:
    decision = evaluate_pair_exit(_pair(), pnl=Decimal(500), now=NOW)
    assert decision is not None
    assert decision.reason == REASON_PAIR_TARGET


def test_pair_stop_beats_target() -> None:
    # Impossible economically, but stop is checked first.
    decision = evaluate_pair_exit(
        _pair(stop=Decimal(1), target=Decimal(1)),
        pnl=Decimal(-5),
        now=NOW,
    )
    assert decision is not None
    assert decision.reason == REASON_PAIR_STOP


def test_pair_percent_units() -> None:
    params = _pair(
        target=Decimal("0.05"),
        stop=Decimal("0.02"),
        target_unit=EXIT_UNIT_PERCENT,
        stop_unit=EXIT_UNIT_PERCENT,
    )
    stop_hit = evaluate_pair_exit(params, pnl=Decimal(-200), now=NOW)
    assert stop_hit is not None
    assert stop_hit.reason == REASON_PAIR_STOP
    assert stop_hit.threshold == Decimal(200)
    target_hit = evaluate_pair_exit(params, pnl=Decimal(500), now=NOW)
    assert target_hit is not None
    assert target_hit.reason == REASON_PAIR_TARGET
    assert target_hit.threshold == Decimal(500)


def test_pair_disabled_thresholds_ignored() -> None:
    params = _pair(target=Decimal(0), stop=Decimal(0), time_limit=0)
    assert evaluate_pair_exit(params, pnl=Decimal(9999), now=NOW) is None
    assert evaluate_pair_exit(params, pnl=Decimal(-9999), now=NOW) is None


def test_pair_time_limit() -> None:
    params = _pair(time_limit=60, opened_at=NOW - timedelta(seconds=61))
    decision = evaluate_pair_exit(params, pnl=Decimal(0), now=NOW)
    assert decision is not None
    assert decision.reason == REASON_PAIR_TIME_LIMIT


def test_pair_time_limit_not_yet() -> None:
    params = _pair(time_limit=3600, opened_at=NOW - timedelta(seconds=10))
    assert evaluate_pair_exit(params, pnl=Decimal(0), now=NOW) is None


def test_pair_time_limit_zero_disabled() -> None:
    params = _pair(time_limit=0, opened_at=NOW - timedelta(days=2))
    assert evaluate_pair_exit(params, pnl=Decimal(0), now=NOW) is None


def test_account_stop_and_target_absolute() -> None:
    stop = evaluate_account_risk(_account(), session_pnl=Decimal(-1000))
    assert stop is not None
    assert stop.reason == REASON_ACCOUNT_STOP
    target = evaluate_account_risk(_account(), session_pnl=Decimal(2000))
    assert target is not None
    assert target.reason == REASON_ACCOUNT_TARGET
    assert evaluate_account_risk(_account(), session_pnl=Decimal(0)) is None


def test_account_percent_of_margin() -> None:
    params = _account(
        daily_target=Decimal("0.02"),
        daily_stop=Decimal("0.01"),
        daily_target_unit=EXIT_UNIT_PERCENT,
        daily_stop_unit=EXIT_UNIT_PERCENT,
    )
    stop = evaluate_account_risk(params, session_pnl=Decimal(-1000))
    assert stop is not None
    assert stop.reason == REASON_ACCOUNT_STOP
    assert stop.threshold == Decimal(1000)
    target = evaluate_account_risk(params, session_pnl=Decimal(2000))
    assert target is not None
    assert target.threshold == Decimal(2000)


def test_account_null_thresholds_disabled() -> None:
    params = _account(daily_target=None, daily_stop=None)
    assert evaluate_account_risk(params, session_pnl=Decimal(-99999)) is None
    assert evaluate_account_risk(params, session_pnl=Decimal(99999)) is None


def test_account_zero_is_breakeven_not_disabled() -> None:
    stop_only = _account(daily_stop=Decimal(0), daily_target=None)
    hit = evaluate_account_risk(stop_only, session_pnl=Decimal(0))
    assert hit is not None
    assert hit.reason == REASON_ACCOUNT_STOP
    assert hit.threshold == Decimal(0)
    assert evaluate_account_risk(stop_only, session_pnl=Decimal("0.01")) is None

    target_only = _account(daily_stop=None, daily_target=Decimal(0))
    hit = evaluate_account_risk(target_only, session_pnl=Decimal(0))
    assert hit is not None
    assert hit.reason == REASON_ACCOUNT_TARGET
    assert hit.threshold == Decimal(0)
    assert evaluate_account_risk(target_only, session_pnl=Decimal("-0.01")) is None
