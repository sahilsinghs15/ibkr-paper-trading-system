"""Incomplete-leg retry tests. IBKR is mocked; extends BasketCoordinator compensation."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.broker.ibkr.gateway_rate_limiter import GatewayRateLimiter
from app.oms.basket import BasketState
from app.oms.coordinator import BasketCoordinator
from app.oms.retry_policy import ExecutionRetryPolicy
from app.rms.checks.base import BaseRMSCheck
from app.rms.engine import RMSEngine
from app.rms.models import (
    CheckResult,
    OrderIntent,
    OrderSide,
    RMSContext,
    RMSOutcome,
    StrategyConfig,
)
from tests.ibkr_test_utils import wire_test_managed_accounts
from tests.test_basket_coordinator import (
    _STRAT,
    PlaceScript,
    _coord,
    _intent,
    _pass,
    _wired,
)


def _ctx() -> RMSContext:
    return RMSContext(
        strategy_configs={
            _STRAT: StrategyConfig(
                strategy_id=_STRAT,
                max_open_positions=100,
                money_limit_per_symbol=Decimal(100000000),
            )
        }
    )


def _retry_coord(oms, **kwargs) -> BasketCoordinator:
    policy = ExecutionRetryPolicy(
        enabled=True,
        square_off_after_sec=0.05,
        max_retries=3,
        retry_interval_sec=0.01,
        retry_window_sec=1.0,
    )
    return BasketCoordinator(
        oms,
        fill_timeout=0.05,
        cancel_timeout=0.05,
        retry_policy=policy,
        rms_engine=RMSEngine(),
        rms_context=_ctx(),
        paper_retries_allowed=True,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_retry_fills_remaining_after_partial() -> None:
    oms, _adapter, script = _wired(PlaceScript(["partial:140", "fill"]))
    intent = _intent(["SIL", "GDX"], trade_id="T-REM", qtys=[275.0, 275.0])
    result = await _retry_coord(oms).execute(intent, _pass(intent), order_type="MARKET")
    assert result.state == BasketState.OPEN
    sil_filled = sum(o.filled_quantity for o in result.orders if o.symbol == "SIL")
    assert sil_filled == pytest.approx(275.0)
    retry_orders = [o for o in result.orders if ":RETRY:" in (o.parent_signal_id or "")]
    assert len(retry_orders) == 1
    assert retry_orders[0].quantity == pytest.approx(135.0)
    assert retry_orders[0].quantity != 275.0
    assert script.place_count == 3


@pytest.mark.asyncio
async def test_retry_count_capped() -> None:
    oms, _adapter, _script = _wired(PlaceScript(["fill", "pending", "pending", "pending", "pending"]))
    intent = _intent(["XLE", "XOP"], trade_id="T-CAP")
    policy = ExecutionRetryPolicy(
        enabled=True,
        square_off_after_sec=0.05,
        max_retries=2,
        retry_interval_sec=0.01,
        retry_window_sec=0.4,
    )
    coord = BasketCoordinator(
        oms,
        fill_timeout=0.05,
        cancel_timeout=0.05,
        retry_policy=policy,
        rms_engine=RMSEngine(),
        rms_context=_ctx(),
        paper_retries_allowed=True,
    )
    await coord.execute(intent, _pass(intent), order_type="MARKET")
    retry_orders = [
        o
        for o in oms.get_all_orders()
        if ":RETRY:" in (o.parent_signal_id or "")
    ]
    assert len(retry_orders) == 2


@pytest.mark.asyncio
async def test_rms_reject_does_not_place_retry() -> None:
    class RejectAll(BaseRMSCheck):
        @property
        def check_number(self) -> int:
            return 99

        @property
        def check_name(self) -> str:
            return "REJECT_ALL"

        def evaluate(self, intent: OrderIntent, context: RMSContext) -> CheckResult:
            return CheckResult(
                check_number=99,
                check_name="REJECT_ALL",
                outcome=RMSOutcome.REJECT,
                reason="TEST_BLOCK",
            )

    oms, _adapter, script = _wired(PlaceScript(["fill", "reject"]))
    intent = _intent(["XLE", "XOP"], trade_id="T-RMS")
    coord = BasketCoordinator(
        oms,
        fill_timeout=0.05,
        cancel_timeout=0.05,
        retry_policy=ExecutionRetryPolicy(
            enabled=True,
            square_off_after_sec=0.05,
            max_retries=3,
            retry_interval_sec=0.01,
            retry_window_sec=0.5,
        ),
        rms_engine=RMSEngine(checks=[RejectAll()]),
        rms_context=_ctx(),
        paper_retries_allowed=True,
    )
    result = await coord.execute(intent, _pass(intent), order_type="MARKET")
    assert script.place_count == 3  # original two + compensation, no retry order placed
    assert result.state == BasketState.COMPENSATED
    assert len(result.compensation_orders) == 1


@pytest.mark.asyncio
async def test_live_port_skips_retries() -> None:
    oms, _adapter, script = _wired(PlaceScript(["fill", "reject"]))
    intent = _intent(["XLE", "XOP"], trade_id="T-LIVE")
    coord = BasketCoordinator(
        oms,
        fill_timeout=0.05,
        cancel_timeout=0.05,
        retry_policy=ExecutionRetryPolicy(
            enabled=True,
            square_off_after_sec=0.05,
            max_retries=3,
            retry_interval_sec=0.01,
            retry_window_sec=0.5,
        ),
        rms_engine=RMSEngine(),
        rms_context=_ctx(),
        paper_retries_allowed=False,
    )
    result = await coord.execute(intent, _pass(intent), order_type="MARKET")
    assert script.place_count == 3  # original two + compensation, no remainder retry
    assert result.state == BasketState.COMPENSATED
    assert result.compensation_orders[0].side == OrderSide.SELL


@pytest.mark.asyncio
async def test_default_coordinator_still_compensates_without_retry() -> None:
    oms, _adapter, script = _wired(PlaceScript(["fill", "reject"]))
    intent = _intent(["XLE", "XOP"], trade_id="T-OLD")
    result = await _coord(oms).execute(intent, _pass(intent), order_type="MARKET")
    assert result.state == BasketState.COMPENSATED
    assert script.place_count == 3


@pytest.mark.asyncio
async def test_duplicate_retry_key_not_resubmitted() -> None:
    oms, _adapter, _script = _wired(PlaceScript(["fill", "pending", "fill"]))
    intent = _intent(["XLE", "XOP"], trade_id="T-IDEM")
    coord = _retry_coord(oms)
    coord._retry_ids.add("1:T-IDEM:1:1")
    result = await coord.execute(intent, _pass(intent), order_type="MARKET")
    retry_orders = [o for o in result.orders if ":RETRY:" in (o.parent_signal_id or "")]
    assert not any(":RETRY:L1:1" in (o.parent_signal_id or "") for o in retry_orders)


@pytest.mark.asyncio
async def test_gateway_rate_limiter_serializes_place_order() -> None:
    import time

    from app.broker.ibkr.tws_client import TWSClient
    from app.oms.ibkr_adapter import IBKRExecutionAdapter
    from app.oms.oms_service import OMSService

    limiter = GatewayRateLimiter(
        max_msg_per_sec=5.0,
        normal_msg_per_sec=5.0,
        emergency_reserve_per_sec=0.0,
        max_wait_sec=5.0,
        error100_cooldown_sec=0.0,
        max_burst=1.0,
    )
    tws = MagicMock(spec=TWSClient)
    tws.is_connected.return_value = True
    tws.next_order_id = 700
    tws.get_request_type.return_value = "order"
    adapter = IBKRExecutionAdapter(client=tws, rate_limiter=limiter)
    adapter.is_connected = lambda: True  # type: ignore[method-assign]
    wire_test_managed_accounts(adapter)
    script = PlaceScript(["fill", "fill", "fill"])
    script.bind(adapter, tws)
    oms = OMSService(adapter=adapter)
    intent = _intent(["A", "B", "C"], trade_id="T-PACE")
    started = time.monotonic()
    result = await _coord(oms).execute(intent, _pass(intent), order_type="MARKET")
    elapsed = time.monotonic() - started
    assert result.state == BasketState.OPEN
    assert elapsed >= 0.30
    assert script.place_count == 3


@pytest.mark.asyncio
async def test_full_fill_does_not_retry() -> None:
    oms, _adapter, script = _wired(PlaceScript(["fill", "fill"]))
    intent = _intent(["XLE", "XOP"], trade_id="T-FULL")
    result = await _retry_coord(oms).execute(intent, _pass(intent), order_type="MARKET")
    assert result.state == BasketState.OPEN
    assert script.place_count == 2
    assert result.compensation_orders == []


@pytest.mark.asyncio
async def test_retry_window_stops_retries_early() -> None:
    oms, _adapter, _script = _wired(PlaceScript(["fill", "pending", "pending", "fill"]))
    intent = _intent(["XLE", "XOP"], trade_id="T-WIN")
    policy = ExecutionRetryPolicy(
        enabled=True,
        square_off_after_sec=0.05,
        max_retries=10,
        retry_interval_sec=0.05,
        retry_window_sec=0.08,
    )
    coord = BasketCoordinator(
        oms,
        fill_timeout=0.05,
        cancel_timeout=0.05,
        retry_policy=policy,
        rms_engine=RMSEngine(),
        rms_context=_ctx(),
        paper_retries_allowed=True,
    )
    result = await coord.execute(intent, _pass(intent), order_type="MARKET")
    assert result.state == BasketState.COMPENSATED
    retry_orders = [o for o in oms.get_all_orders() if ":RETRY:" in (o.parent_signal_id or "")]
    assert len(retry_orders) < 10


@pytest.mark.asyncio
async def test_multi_leg_both_partially_filled_retries_remainders() -> None:
    oms, _adapter, _script = _wired(PlaceScript(["partial:100", "partial:50", "fill", "fill"]))
    intent = _intent(["SIL", "GDX"], trade_id="T-MULTI-REM", qtys=[200.0, 100.0])
    result = await _retry_coord(oms).execute(intent, _pass(intent), order_type="MARKET")
    assert result.state == BasketState.OPEN
    sil_retry = [o for o in result.orders if o.symbol == "SIL" and ":RETRY:" in (o.parent_signal_id or "")]
    gdx_retry = [o for o in result.orders if o.symbol == "GDX" and ":RETRY:" in (o.parent_signal_id or "")]
    assert len(sil_retry) == 1
    assert sil_retry[0].quantity == pytest.approx(100.0)
    assert len(gdx_retry) == 1
    assert gdx_retry[0].quantity == pytest.approx(50.0)


def test_retry_policy_validation_rejects_invalid_config() -> None:
    with pytest.raises(ValueError, match="square_off_after_sec must be greater than 0"):
        ExecutionRetryPolicy(square_off_after_sec=0).validate()
    with pytest.raises(ValueError, match="max_retries must be >= 0"):
        ExecutionRetryPolicy(max_retries=-1).validate()
    with pytest.raises(ValueError, match="retry_interval_sec must be greater than 0"):
        ExecutionRetryPolicy(retry_interval_sec=0).validate()
    with pytest.raises(ValueError, match="retry_window_sec must be >= retry_interval_sec"):
        ExecutionRetryPolicy(retry_interval_sec=10, retry_window_sec=5).validate()

