"""Calculation helpers for daily estimate and monthly ledger."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any


def get_month_bounds(today: date) -> tuple[date, date]:
    """Return (month_start, today) for monthly range."""
    month_start = date(today.year, today.month, 1)
    return month_start, today


def compute_credit_usage(
    *,
    today: date,
    actual_records: list[dict[str, Any]],
    latest_actual_total: Decimal | None,
    latest_actual_date: date | None,
    stale_days: int = 3,
    fetched_at: datetime | None = None,
) -> dict[str, Any]:
    """Compute System Monitor credit response from persisted actuals.

    - actual_records: list of {usage_date, total_cost_usd} with status ACTUAL
      for current month (usage_date >= month_start and < today)
    - latest_actual_total: latest reliable completed-day actual total (or None)
    - today: current UTC date

    Returns dict matching DailyCreditUsage fields (see schema).
    Estimates never double-count: actual sum is for dates < today; estimate is for today only.
    """
    month_start, _ = get_month_bounds(today)

    # Sum actuals for completed days in current month (exclude today if somehow present)
    actual_total = Decimal(0)
    actual_through: date | None = None
    for rec in actual_records:
        d = rec["usage_date"] if isinstance(rec["usage_date"], date) else date.fromisoformat(str(rec["usage_date"]))
        if d >= month_start and d < today:
            amt = rec.get("total_cost_usd")
            if amt is not None:
                if not isinstance(amt, Decimal):
                    amt = Decimal(str(amt))
                actual_total += amt
                if actual_through is None or d > actual_through:
                    actual_through = d

    # Determine estimate for today
    estimate_date = today
    current_estimate: Decimal | None = None
    estimate_source_date = latest_actual_date

    if latest_actual_total is not None:
        current_estimate = latest_actual_total
    else:
        current_estimate = None

    # Monthly displayed total = actual_total + current_estimate (if available)
    if current_estimate is not None:
        displayed_monthly = actual_total + current_estimate
    else:
        displayed_monthly = actual_total if actual_records else None
        # If no actuals at all and no estimate, monthly is None (unavailable)

    # Stale detection: if latest actual is older than stale_days, mark stale
    is_stale = False
    if latest_actual_date is not None:
        delta = (today - latest_actual_date).days
        if delta > stale_days:
            is_stale = True
    elif not actual_records:
        is_stale = True

    # Human readable range
    # e.g., "1 Sep – 15 Sep 2026" — frontend formats, so expose raw dates
    return {
        "month_start": month_start,
        "month_end": today,
        "actual_total": actual_total,
        "actual_through": actual_through,
        "current_estimate": current_estimate,
        "estimate_date": estimate_date if current_estimate is not None else None,
        "estimate_source_date": estimate_source_date,
        "displayed_monthly_total": displayed_monthly,
        "is_stale": is_stale,
        "fetched_at": fetched_at,
    }


def format_usd(amount: Decimal | None) -> str | None:
    if amount is None:
        return None
    # 2 decimal places
    return f"${amount:.2f}"


def daily_display(
    *,
    today: date,
    latest_record: dict[str, Any] | None,
    latest_actual_total: Decimal | None,
    latest_actual_date: date | None,
) -> dict[str, Any]:
    """Return daily instance cost display for System Monitor.

    For current day: status ESTIMATE, amount = latest actual.
    For completed day detail, caller may query ledger directly.
    """
    if latest_record is None and latest_actual_total is None:
        return {"amount": None, "status": "UNAVAILABLE", "date": today, "source_date": None}

    # If ledger has ACTUAL for today, show ACTUAL
    if latest_record is not None:
        rec_date = latest_record["usage_date"] if isinstance(latest_record["usage_date"], date) else date.fromisoformat(str(latest_record["usage_date"]))
        if rec_date == today and latest_record.get("status") == "ACTUAL":
            return {
                "amount": latest_record.get("total_cost_usd"),
                "status": "ACTUAL",
                "date": today,
                "source_date": today,
            }

    # Otherwise estimate
    return {
        "amount": latest_actual_total,
        "status": "ESTIMATE" if latest_actual_total is not None else "UNAVAILABLE",
        "date": today,
        "source_date": latest_actual_date,
    }
