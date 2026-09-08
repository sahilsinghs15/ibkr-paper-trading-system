"""Tests for multi-account reject_reason parse / scope / merge."""

from app.services.account_reject_reason import (
    has_account_prefixed_segments,
    merge_account_reject_reasons,
    parse_account_reject_segments,
    scope_reject_reason_for_account,
)


def test_parse_and_scope_fanout_reject_blob() -> None:
    raw = (
        "Account DUR919062: MARGIN_SNAPSHOT_STALE: snapshot for DUR919062 older than 300s.; "
        "Account U7211090: MODEL_BLUE_MIN_NOTIONAL: JETS notional 28.47 is below minimum 100."
    )
    segments = parse_account_reject_segments(raw)
    assert "DUR919062" in segments
    assert "U7211090" in segments
    assert "MARGIN_SNAPSHOT_STALE" in segments["DUR919062"]
    assert "MODEL_BLUE_MIN_NOTIONAL" in segments["U7211090"]

    dur = scope_reject_reason_for_account(raw, ibkr_account="DUR919062")
    u721 = scope_reject_reason_for_account(raw, ibkr_account="U7211090")
    assert dur is not None and "MARGIN_SNAPSHOT_STALE" in dur
    assert "U7211090" not in (dur or "")
    assert u721 is not None and "MODEL_BLUE_MIN_NOTIONAL" in u721
    assert "DUR919062" not in (u721 or "")
    assert scope_reject_reason_for_account(raw, ibkr_account="OTHER") is None


def test_unscoped_reason_passes_through() -> None:
    raw = "RMS Risk Limit Exceeded"
    assert not has_account_prefixed_segments(raw)
    assert scope_reject_reason_for_account(raw, ibkr_account="DUR919062") == raw


def test_merge_preserves_sibling_account_reasons() -> None:
    first = "Account DUR919062: MARGIN_SNAPSHOT_STALE: stale."
    second = "Account U7211090: MODEL_BLUE_MIN_NOTIONAL: too small."
    merged = merge_account_reject_reasons(first, second)
    assert merged is not None
    assert "DUR919062" in merged
    assert "U7211090" in merged
    # Second write for same account replaces that segment only.
    updated = merge_account_reject_reasons(merged, "Account DUR919062: OPEN_POSITION_LIMIT_REACHED")
    assert updated is not None
    assert "OPEN_POSITION_LIMIT_REACHED" in updated
    assert "MARGIN_SNAPSHOT_STALE" not in updated
    assert "U7211090" in updated
