"""Manual Trading order polling must be state-driven, not unconditional.

Since the Manual Trading workspace redesign the order data hook lives in
frontend/src/components/manual/useManualWorkspace.ts and the page wires
refreshes in frontend/src/pages/ManualTradePage.tsx. These tests assert the
same invariants against those files.
"""

import pathlib
import re

_FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src"


def _read(rel: str) -> str:
    return (_FRONTEND / rel).read_text()


def _hook() -> str:
    return _read("components/manual/useManualWorkspace.ts")


def _page() -> str:
    return _read("pages/ManualTradePage.tsx")


def _blotter() -> str:
    return _read("components/manual/ManualOrdersBlotter.tsx")


def _active_statuses() -> list[str]:
    m = re.search(r"ACTIVE_ORDER_STATUSES\s*=\s*\[([^\]]*)\]", _hook())
    assert m, "ACTIVE_ORDER_STATUSES must be declared in the order hook"
    return re.findall(r"'([A-Z_]+)'", m.group(1))


def test_no_unconditional_polling():
    """Exactly one interval in the manual trading workspace, gated on working orders."""
    workspace = _hook() + _page() + _blotter() + _read("components/manual/ManualPositionsPanel.tsx")
    assert workspace.count("setInterval") == 1, "Exactly one polling interval (state-driven)"
    hook = _hook()
    assert "if (!hasActive) return" in hook
    interval_pos = hook.index("setInterval")
    assert hook.rfind("if (!hasActive) return", 0, interval_pos) != -1, "Guard must precede the interval"


def test_polling_active_states():
    """Polling is active exactly for PENDING_SUBMIT, SUBMITTED, PARTIALLY_FILLED."""
    assert sorted(_active_statuses()) == ["PARTIALLY_FILLED", "PENDING_SUBMIT", "SUBMITTED"]
    assert "orders.some((o) => ACTIVE_ORDER_STATUSES.includes(o.status))" in _hook()


def test_polling_stops_for_terminal():
    """Terminal statuses never keep the interval alive."""
    statuses = _active_statuses()
    for terminal in ("FILLED", "CANCELLED", "REJECTED", "ERROR"):
        assert terminal not in statuses, f"Terminal {terminal} must not trigger polling"


def test_manual_refresh_still_works():
    """A workspace Refresh button reloads orders (and positions)."""
    page = _page()
    assert "onClick={refreshAll}" in page
    refresh_all = page[page.index("const refreshAll"):]
    refresh_all = refresh_all[: refresh_all.index("}") + 1]
    assert "orders.reload()" in refresh_all
    assert "positions.reload()" in refresh_all


def test_immediate_refresh_after_submit_and_cancel():
    """Orders refresh immediately after a submit and after a cancel."""
    page = _page()
    submitted = page[page.index("onSubmitted={"):]
    submitted = submitted[: submitted.index("}}") + 2]
    assert "orders.reload(1)" in submitted
    cancelled = page[page.index("onCancelled={"):]
    cancelled = cancelled[: cancelled.index("}}") + 2]
    assert "orders.reload()" in cancelled
    blotter = _blotter()
    assert "onCancelled()" in blotter[blotter.index("const handleCancel"):]


def test_cleanup_on_unmount_and_account_change():
    """The interval is cleared on unmount and re-created when deps change."""
    hook = _hook()
    assert "return () => clearInterval(t)" in hook
    assert "}, [hasActive, reload, page])" in hook
    # reload is rebuilt when the account changes, so the interval is recreated per account.
    assert "[account, page]" in hook
