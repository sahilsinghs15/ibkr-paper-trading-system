"""Tests for multi-account reject_reason parse / scope / merge."""

from datetime import UTC, datetime

from app.oms.models import (
    AccountExecutionOutcome,
    ExecutionResult,
    FanoutExecutionResult,
    OMSOrder,
    OMSOrderStatus,
)
from app.rms.models import OrderAction, OrderIntent, OrderSide, RMSOutcome, RMSResult
from app.services.account_reject_reason import (
    GENERIC_REJECT_FALLBACK,
    collect_fanout_reject_reasons,
    has_account_prefixed_segments,
    is_generic_reject_reason,
    merge_account_reject_reasons,
    parse_account_reject_segments,
    resolve_reject_source,
    scope_reject_reason_for_account,
)


def _intent(**kwargs) -> OrderIntent:
    return OrderIntent(
        signal_id=kwargs.get("signal_id", "SIG-1"),
        strategy_id=kwargs.get("strategy_id", "model_blue"),
        action=kwargs.get("action", OrderAction.OPEN),
        legs=[],
        account_id=kwargs.get("account_id"),
        ibkr_account=kwargs.get("ibkr_account"),
        timestamp=datetime.now(UTC),
    )


def _order(**kwargs) -> OMSOrder:
    intent = kwargs.get("intent") or _intent(
        account_id=kwargs.get("account_id"),
        ibkr_account=kwargs.get("ibkr_account"),
    )
    return OMSOrder(
        internal_order_id=kwargs.get("internal_order_id", "ORD-1"),
        intent=intent,
        symbol=kwargs.get("symbol", "EWC"),
        side=kwargs.get("side", OrderSide.BUY),
        quantity=kwargs.get("quantity", 10),
        status=kwargs.get("status", OMSOrderStatus.REJECTED),
        error_message=kwargs.get("error_message"),
    )


def _outcome(**kwargs) -> AccountExecutionOutcome:
    intent = _intent(
        account_id=kwargs.get("account_id", 452),
        ibkr_account=kwargs.get("ibkr_account", "U7211090"),
    )
    rms = RMSResult(
        outcome=kwargs.get("rms_outcome", RMSOutcome.PASS),
        intent=intent,
        original_intent=intent,
        reason=kwargs.get("rms_reason"),
    )
    orders = kwargs.get("orders")
    if orders is None and kwargs.get("error_message"):
        orders = [_order(error_message=kwargs["error_message"], intent=intent)]
    result = ExecutionResult(
        order=orders[0] if orders else _order(intent=intent),
        rms_result=rms,
        success=kwargs.get("success", False),
        orders=orders or [],
        error_message=kwargs.get("result_error_message"),
    )
    return AccountExecutionOutcome(
        account_id=kwargs.get("account_id", 452),
        ibkr_account=kwargs.get("ibkr_account", "U7211090"),
        result=result,
        error=kwargs.get("error"),
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


def test_collect_fanout_reject_reasons_rms_only() -> None:
    outcome = _outcome(rms_outcome=RMSOutcome.REJECT, rms_reason="MODEL_BLUE_MIN_SHARE: EWC below 1 share")
    res = FanoutExecutionResult(outcomes=[outcome])
    msg = collect_fanout_reject_reasons(res)
    assert "MODEL_BLUE_MIN_SHARE" in msg
    assert "Account U7211090:" in msg


def test_collect_fanout_reject_reasons_sizer_value_error() -> None:
    outcome = AccountExecutionOutcome(
        account_id=452,
        ibkr_account="U7211090",
        result=None,
        error="NO_OPEN_POSITION: no position for trade T-1",
    )
    res = FanoutExecutionResult(outcomes=[outcome])
    msg = collect_fanout_reject_reasons(res)
    assert "NO_OPEN_POSITION" in msg
    assert "Account U7211090:" in msg


def test_collect_fanout_reject_reasons_broker_order_error() -> None:
    broker_msg = "TWS Error 201: Client Portal token verification required"
    outcome = _outcome(
        error_message=broker_msg,
        result_error_message="COMPENSATED",
    )
    res = FanoutExecutionResult(outcomes=[outcome])
    msg = collect_fanout_reject_reasons(res)
    assert "TWS Error 201" in msg
    assert "COMPENSATED" not in msg
    assert msg != GENERIC_REJECT_FALLBACK


def test_collect_fanout_reject_reasons_basket_state_only_uses_fallback() -> None:
    outcome = _outcome(result_error_message="COMPENSATED", orders=[])
    res = FanoutExecutionResult(outcomes=[outcome])
    assert collect_fanout_reject_reasons(res) == GENERIC_REJECT_FALLBACK


def test_collect_fanout_skips_disconnect_park_message() -> None:
    parked = _order(error_message="Connection closed unexpectedly; order parked")
    live = _order(internal_order_id="ORD-2", error_message="TWS Error 201: verify token")
    outcome = _outcome(orders=[parked, live], result_error_message="COMPENSATED")
    res = FanoutExecutionResult(outcomes=[outcome])
    msg = collect_fanout_reject_reasons(res)
    assert "TWS Error 201" in msg
    assert "Connection closed unexpectedly" not in msg


def test_is_generic_reject_reason_and_resolve_reject_source() -> None:
    generic = GENERIC_REJECT_FALLBACK
    specific = "Account U7211090: TWS Error 201: verify"
    assert is_generic_reject_reason(generic)
    assert not is_generic_reject_reason(specific)
    assert resolve_reject_source(generic, specific) == specific
    assert resolve_reject_source(specific, generic) == specific
    assert resolve_reject_source(None, None) is None
