"""Tests for ManualTrading polling fix - state-driven vs unconditional."""

import pathlib

import pytest


def _get_manual_page_text() -> str:
    p = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages" / "ManualTradePage.tsx"
    if not p.exists():
        p = pathlib.Path("/home/dev3/Documents/ibkr-paper-trading-system/frontend/src/pages/ManualTradePage.tsx")
    return p.read_text()


def test_no_unconditional_polling():
    """Should not have unconditional setInterval every 5000ms without active check."""
    text = _get_manual_page_text()
    # Old code had unconditional: useEffect(() => { void loadManualOrders(); const t = setInterval(...,5000); return () => clearInterval(t)}, [loadManualOrders])
    # New code should have hasActiveOrders guard
    assert "hasActiveOrders" in text, "Polling should be gated by hasActiveOrders"
    # Should not have old unconditional pattern
    # Count setInterval occurrences - should be 1 with guard, not 2
    assert text.count("setInterval") == 1, "Should have exactly one polling interval (state-driven)"
    assert "if (!hasActiveOrders) return" in text or "if (!hasActiveOrders)" in text


def test_polling_active_states():
    """Polling should be active for PENDING_SUBMIT, SUBMITTED, PARTIALLY_FILLED."""
    text = _get_manual_page_text()
    for status in ["PENDING_SUBMIT", "SUBMITTED", "PARTIALLY_FILLED"]:
        assert status in text, f"Active status {status} should be checked for polling"


def test_polling_stops_for_terminal():
    """Polling should stop for FILLED, CANCELLED, REJECTED, ERROR (no interval when all terminal)."""
    text = _get_manual_page_text()
    has_active_line = [line for line in text.splitlines() if "hasActiveOrders" in line and "some" in line]
    assert has_active_line, "hasActiveOrders should use some with active statuses"
    # Active statuses must be exactly PENDING_SUBMIT, SUBMITTED, PARTIALLY_FILLED
    assert "PENDING_SUBMIT" in has_active_line[0]
    assert "SUBMITTED" in has_active_line[0]
    assert "PARTIALLY_FILLED" in has_active_line[0]
    # Terminal statuses should not be listed as active (check quoted exact)
    for term in ['"FILLED"', '"CANCELLED"', '"REJECTED"', '"ERROR"']:
        # Count occurrences outside of PARTIALLY_FILLED
        # Remove PARTIALLY_FILLED first to avoid false positive
        line_without_partial = has_active_line[0].replace("PARTIALLY_FILLED", "")
        assert term not in line_without_partial, f"Terminal {term} should not trigger polling"


def test_manual_refresh_still_works():
    """Manual Refresh button should still exist and call loadManualOrders."""
    text = _get_manual_page_text()
    assert "Refresh" in text
    assert "loadManualOrders" in text
    assert "onClick={() => void loadManualOrders()" in text or "onClick={() => void loadManualOrders" in text


def test_immediate_refresh_after_submit_and_cancel():
    """After submit/cancel, should do immediate refresh."""
    text = _get_manual_page_text()
    # handleConfirmSubmit and handleCancelOrder should call loadManualOrders
    assert "void loadManualOrders()" in text
    # Should be called after submit and cancel
    assert text.count("void loadManualOrders()") >= 2, "Should refresh after submit and cancel"


def test_cleanup_on_unmount_and_account_change():
    """Polling timer should be cleaned up on unmount and account change."""
    text = _get_manual_page_text()
    assert "return () => clearInterval" in text
    assert "loadManualOrders" in text
    # Should depend on hasActiveOrders and loadManualOrders
    assert "hasActiveOrders" in text and "loadManualOrders" in text
