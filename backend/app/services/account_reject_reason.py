"""Parse / scope / merge multi-account signal reject_reason strings.

Fan-out writes a shared ``signals.reject_reason`` that may concatenate
``Account {ibkr}: …`` segments. Dashboards must show only the open account's
segment; concurrent scoped jobs must merge rather than overwrite siblings.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.oms.models import AccountExecutionOutcome, FanoutExecutionResult, OMSOrder

GENERIC_REJECT_FALLBACK = "Execution rejected by RMS/OMS policy"

_BASKET_STATE_NAMES = frozenset(
    {
        "PENDING",
        "EXECUTING",
        "OPEN",
        "CLOSED",
        "UNWINDING",
        "COMPENSATED",
        "CRITICAL",
        "RECOVERED",
    }
)

_ACCOUNT_SEGMENT = re.compile(
    r"Account\s+(\S+?):\s*(.*?)(?=(?:;\s*)?Account\s+\S+?:|\Z)",
    re.DOTALL | re.IGNORECASE,
)


def parse_account_reject_segments(raw: str | None) -> dict[str, str]:
    """Return ``{account_label_upper: reason}`` for Account-prefixed segments."""
    if not raw or not str(raw).strip():
        return {}
    text = str(raw).strip()
    out: dict[str, str] = {}
    for match in _ACCOUNT_SEGMENT.finditer(text):
        label = match.group(1).strip().upper()
        reason = match.group(2).strip().rstrip(" ;")
        if label and reason:
            out[label] = reason
    return out


def has_account_prefixed_segments(raw: str | None) -> bool:
    return bool(parse_account_reject_segments(raw))


def scope_reject_reason_for_account(
    raw: str | None,
    *,
    ibkr_account: str | None = None,
    account_id: int | None = None,
) -> str | None:
    """Return this account's reject text, or the raw string if unscoped.

    When the blob is multi-account (``Account X: …; Account Y: …``) and none of
    the segments match ``ibkr_account`` / ``account_id``, returns ``None`` so
    callers do not surface a sibling account's rejection.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None

    segments = parse_account_reject_segments(text)
    if not segments:
        return text

    keys: list[str] = []
    if ibkr_account and str(ibkr_account).strip():
        keys.append(str(ibkr_account).strip().upper())
    if account_id is not None:
        keys.append(str(account_id))

    for key in keys:
        if key in segments:
            return segments[key]
    return None


def format_account_reject_reason(
    ibkr_account: str | None,
    reason: str | None,
    *,
    account_id: int | None = None,
) -> str | None:
    """Ensure a reject reason is tagged with ``Account {ibkr}:`` for merge/scope."""
    if reason is None or not str(reason).strip():
        return None
    text = str(reason).strip()
    if has_account_prefixed_segments(text):
        return text
    label = (ibkr_account or "").strip() or (
        str(account_id) if account_id is not None else ""
    )
    if not label:
        return text
    return f"Account {label}: {text}"


def merge_account_reject_reasons(
    existing: str | None,
    incoming: str | None,
) -> str | None:
    """Merge Account-prefixed reject segments; last write wins per account label."""
    if not incoming or not str(incoming).strip():
        return existing if existing and str(existing).strip() else None
    incoming_text = str(incoming).strip()
    incoming_segments = parse_account_reject_segments(incoming_text)
    existing_segments = parse_account_reject_segments(existing)

    if not incoming_segments and not existing_segments:
        return incoming_text
    if not incoming_segments:
        # Unscoped incoming replaces a prior unscoped reason, but must not wipe
        # structured multi-account history.
        if existing_segments:
            return _format_segments(existing_segments)
        return incoming_text

    merged = dict(existing_segments)
    merged.update(incoming_segments)
    return _format_segments(merged)


def _format_segments(segments: dict[str, str]) -> str:
    # Stable order by label for readable diffs / tests.
    parts = [f"Account {label}: {reason}" for label, reason in sorted(segments.items())]
    return "; ".join(parts)


def is_generic_reject_reason(reason: str | None) -> bool:
    """True when the text is empty or the legacy worker/order_manager fallback."""
    if reason is None:
        return True
    return str(reason).strip() == GENERIC_REJECT_FALLBACK


def _is_basket_state_error(msg: str) -> bool:
    return msg.strip().upper() in _BASKET_STATE_NAMES


def _first_order_broker_error(orders: list[OMSOrder] | None) -> str | None:
    for order in orders or []:
        msg = getattr(order, "error_message", None)
        if not msg or not str(msg).strip():
            continue
        text = str(msg).strip()
        if text.startswith("Connection closed unexpectedly"):
            continue
        return text
    return None


def _outcome_reject_reason(outcome: AccountExecutionOutcome) -> str | None:
    if outcome.error:
        return str(outcome.error).strip() or None
    result = outcome.result
    if result is None:
        return None
    rms = getattr(result, "rms_result", None)
    if rms is not None and getattr(rms, "reason", None):
        reason = str(rms.reason).strip()
        if reason:
            return reason
    broker = _first_order_broker_error(getattr(result, "orders", None))
    if broker:
        return broker
    err_msg = getattr(result, "error_message", None)
    if err_msg and str(err_msg).strip():
        text = str(err_msg).strip()
        if not _is_basket_state_error(text):
            return text
    return None


def collect_fanout_reject_reasons(res: FanoutExecutionResult) -> str:
    """Build a merged Account-prefixed reject_reason from a fan-out execution result."""
    merged: str | None = None
    for outcome in getattr(res, "outcomes", []) or []:
        raw = _outcome_reject_reason(outcome)
        if not raw:
            continue
        formatted = format_account_reject_reason(
            outcome.ibkr_account,
            raw,
            account_id=outcome.account_id,
        )
        if formatted:
            merged = merge_account_reject_reasons(merged, formatted)
    return merged or GENERIC_REJECT_FALLBACK


def resolve_reject_source(
    job_reject: str | None,
    signal_reject: str | None,
) -> str | None:
    """Prefer a specific job or signal reject over the generic fallback."""
    job_specific = job_reject if job_reject and not is_generic_reject_reason(job_reject) else None
    sig_specific = (
        signal_reject if signal_reject and not is_generic_reject_reason(signal_reject) else None
    )
    if job_specific:
        return job_specific
    if sig_specific:
        return sig_specific
    return job_reject or signal_reject
