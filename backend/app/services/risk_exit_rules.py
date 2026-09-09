"""Pure pair- and account-level risk-exit evaluation. No I/O."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

EXIT_UNIT_ABSOLUTE = "ABSOLUTE"
EXIT_UNIT_PERCENT = "PERCENT"
VALID_EXIT_UNITS = frozenset({EXIT_UNIT_ABSOLUTE, EXIT_UNIT_PERCENT})

REASON_PAIR_TARGET = "PAIR_TARGET"
REASON_PAIR_STOP = "PAIR_STOP"
REASON_PAIR_TIME_LIMIT = "PAIR_TIME_LIMIT"
REASON_ACCOUNT_TARGET = "ACCOUNT_TARGET"
REASON_ACCOUNT_STOP = "ACCOUNT_STOP"

ZERO = Decimal(0)


@dataclass(frozen=True)
class PairExitParams:
    """Frozen snapshot of a pair's exit thresholds (from the positions row)."""

    target: Decimal
    stop: Decimal
    time_limit: int
    target_unit: str
    stop_unit: str
    opened_at: datetime
    entry_gross_notional: Decimal


@dataclass(frozen=True)
class AccountRiskParams:
    """Live account daily-risk thresholds."""

    daily_target: Decimal | None
    daily_stop: Decimal | None
    daily_target_unit: str
    daily_stop_unit: str
    total_margin: Decimal


@dataclass(frozen=True)
class ExitDecision:
    """Resolved currency threshold that fired."""

    reason: str
    threshold: Decimal
    pnl: Decimal


def pair_entry_gross_notional(
    *,
    leg_a_signed_qty: Decimal,
    leg_a_entry_mark: Decimal,
    leg_b_signed_qty: Decimal | None,
    leg_b_entry_mark: Decimal | None,
) -> Decimal:
    """Gross notional at entry: |qty * mark| summed across both legs."""
    total = abs(leg_a_signed_qty * leg_a_entry_mark)
    if leg_b_signed_qty is not None and leg_b_entry_mark is not None:
        total += abs(leg_b_signed_qty * leg_b_entry_mark)
    return total


def resolve_threshold(
    magnitude: Decimal | None,
    unit: str,
    *,
    basis: Decimal,
    zero_disables: bool = True,
) -> Decimal | None:
    """Convert a stored magnitude into a currency amount.

    NULL disables. Negative values are rejected. When ``zero_disables`` is
    true (pair thresholds), 0 also disables. Account daily stop/target keep
    0 as breakeven. PERCENT is a fraction of ``basis`` in [0, 1].
    Returns None when the threshold is disabled or cannot be resolved.
    """
    if magnitude is None:
        return None
    value = Decimal(str(magnitude))
    if value < ZERO:
        return None
    if value == ZERO and zero_disables:
        return None
    normalized = (unit or EXIT_UNIT_ABSOLUTE).upper()
    if normalized == EXIT_UNIT_ABSOLUTE:
        return value
    if normalized == EXIT_UNIT_PERCENT:
        if basis <= ZERO:
            return None
        return value * basis
    return None


def evaluate_pair_exit(
    params: PairExitParams,
    *,
    pnl: Decimal,
    now: datetime,
) -> ExitDecision | None:
    """Return PAIR_STOP / PAIR_TARGET / PAIR_TIME_LIMIT, or None.

    Stop is checked before target. Time limit fires only if neither PnL
    threshold has already fired. ``time_limit <= 0`` disables the timer.
    """
    stop_amt = resolve_threshold(
        params.stop, params.stop_unit, basis=params.entry_gross_notional
    )
    if stop_amt is not None and pnl <= -stop_amt:
        return ExitDecision(reason=REASON_PAIR_STOP, threshold=stop_amt, pnl=pnl)

    target_amt = resolve_threshold(
        params.target, params.target_unit, basis=params.entry_gross_notional
    )
    if target_amt is not None and pnl >= target_amt:
        return ExitDecision(reason=REASON_PAIR_TARGET, threshold=target_amt, pnl=pnl)

    if params.time_limit > 0:
        opened = params.opened_at
        if opened.tzinfo is None and now.tzinfo is not None:
            opened = opened.replace(tzinfo=now.tzinfo)
        elif opened.tzinfo is not None and now.tzinfo is None:
            now = now.replace(tzinfo=opened.tzinfo)
        if now >= opened + timedelta(seconds=int(params.time_limit)):
            return ExitDecision(
                reason=REASON_PAIR_TIME_LIMIT,
                threshold=Decimal(params.time_limit),
                pnl=pnl,
            )
    return None


def evaluate_account_risk(
    params: AccountRiskParams,
    *,
    session_pnl: Decimal,
) -> ExitDecision | None:
    """Return ACCOUNT_STOP / ACCOUNT_TARGET, or None. Stop is checked first.

    NULL disables a side. 0 is breakeven (stop at ``pnl <= 0``, target at
    ``pnl >= 0``). The account_risk_enabled flag is the on/off switch.
    """
    stop_amt = resolve_threshold(
        params.daily_stop,
        params.daily_stop_unit,
        basis=params.total_margin,
        zero_disables=False,
    )
    if stop_amt is not None and session_pnl <= -stop_amt:
        return ExitDecision(
            reason=REASON_ACCOUNT_STOP, threshold=stop_amt, pnl=session_pnl
        )

    target_amt = resolve_threshold(
        params.daily_target,
        params.daily_target_unit,
        basis=params.total_margin,
        zero_disables=False,
    )
    if target_amt is not None and session_pnl >= target_amt:
        return ExitDecision(
            reason=REASON_ACCOUNT_TARGET, threshold=target_amt, pnl=session_pnl
        )
    return None
