"""Parse / scope / merge multi-account signal reject_reason strings.

Fan-out writes a shared ``signals.reject_reason`` that may concatenate
``Account {ibkr}: …`` segments. Dashboards must show only the open account's
segment; concurrent scoped jobs must merge rather than overwrite siblings.
"""

from __future__ import annotations

import re

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
