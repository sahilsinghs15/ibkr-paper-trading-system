"""Gate A: pre-sizing free-margin gate in _fanout_single_account."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.accounts.context import AccountExecutionContext
from app.models.signal import Signal, SignalType
from app.oms.models import ExecutionResult
from app.rms.models import (
    ExecutionIntentMode,
    MarginPolicy,
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
    RMSContext,
    RMSOutcome,
    RMSResult,
)
from app.services.account_margin import AccountMarginSnapshot
from app.services.order_manager import OrderManager


def _acct(ibkr: str, account_id: int = 1) -> AccountExecutionContext:
    return AccountExecutionContext(
        account_id=account_id,
        ibkr_account=ibkr,
        strategy_id="MODEL_BLUE",
        total_margin=Decimal(100000),
        alloc_pct=Decimal(1),
        committed_notional=Decimal(10000),
        pair_max_allocation_pct=Decimal("1"),
        pair_budget=Decimal(10000),
        target=Decimal(500),
        stop=Decimal(250),
        time_limit=3600,
        max_open_positions=10,
    )


def _signal(*, action: str = "OPEN") -> Signal:
    return Signal(
        signal_type=SignalType.BUY,
        timestamp=datetime.now(UTC),
        reason="test",
        signal_id="SIG-G",
        strategy_id="MODEL_BLUE",
        action=action,
    )


def _snap(**kwargs) -> AccountMarginSnapshot:
    fields = {
        "ibkr_account": "DU1",
        "as_of": datetime.now(UTC),
        "net_liquidation": Decimal(100000),
        "available_funds": Decimal(10000),
        "look_ahead_available_funds": Decimal(10000),
        "max_age_sec": 300,
    }
    fields.update(kwargs)
    return AccountMarginSnapshot(**fields)


def _policy(**kwargs) -> MarginPolicy:
    fields = {
        "check_enabled": True,
        "min_free_buffer": Decimal(0),
        "min_free_pct_of_netliq": Decimal("0.05"),
        "comfort_ratio": Decimal("0.80"),
        "enforce_look_ahead": True,
        "reject_on_stale_snapshot": True,
    }
    fields.update(kwargs)
    return MarginPolicy(**fields)


def _mgr(policy: MarginPolicy, snapshots: dict[str, AccountMarginSnapshot]) -> OrderManager:
    am = MagicMock()
    am.snapshot_for.return_value = None
    context = RMSContext(margin_policy=policy, margin_snapshots=snapshots)
    return OrderManager(oms=None, rms_context=context, account_margin=am)


def _handler(intent: OrderIntent | None = None) -> MagicMock:
    handler = MagicMock()
    handler.build_intent = AsyncMock(
        return_value=intent
        or OrderIntent(
            signal_id="SIG-G",
            strategy_id="MODEL_BLUE",
            action=OrderAction.OPEN,
            ibkr_account="DU1",
            legs=[
                OrderLeg(
                    symbol="AAPL",
                    side=OrderSide.BUY,
                    quantity=1,
                    price=Decimal(100),
                    contract_month="2026-09",
                )
            ],
        )
    )
    return handler


@pytest.mark.asyncio
async def test_below_floor_rejects_before_build_intent() -> None:
    mgr = _mgr(_policy(), {"DU1": _snap(available_funds=Decimal(1000))})
    handler = _handler()
    with pytest.raises(ValueError, match="MARGIN_NO_FREE_FUNDS"):
        await mgr._fanout_single_account(_signal(), handler, _acct("DU1"), raise_if_single=True)
    handler.build_intent.assert_awaited()


@pytest.mark.asyncio
async def test_look_ahead_short_rejects_unless_disabled() -> None:
    snap = _snap(
        available_funds=Decimal(20000),
        look_ahead_available_funds=Decimal(100),
        look_ahead_next_change=datetime.now(UTC) + timedelta(hours=1),
    )
    mgr = _mgr(_policy(enforce_look_ahead=True), {"DU1": snap})
    handler = _handler()
    with pytest.raises(ValueError, match="MARGIN_NO_FREE_FUNDS_LOOK_AHEAD"):
        await mgr._fanout_single_account(_signal(), handler, _acct("DU1"), raise_if_single=True)
    handler.build_intent.assert_awaited()

    mgr2 = _mgr(_policy(enforce_look_ahead=False), {"DU1": snap})
    handler2 = _handler()
    mgr2._evaluate_and_submit = AsyncMock(
        return_value=ExecutionResult(
            order=MagicMock(),
            rms_result=RMSResult(
                outcome=RMSOutcome.PASS,
                intent=handler2.build_intent.return_value,
                original_intent=handler2.build_intent.return_value,
                check_results=[],
            ),
            success=True,
        )
    )
    outcome = await mgr2._fanout_single_account(
        _signal(), handler2, _acct("DU1"), raise_if_single=True
    )
    handler2.build_intent.assert_awaited()
    assert outcome.error is None


@pytest.mark.asyncio
async def test_stale_snapshot_rejects_unless_disabled() -> None:
    snap = _snap(as_of=datetime.now(UTC) - timedelta(seconds=400), max_age_sec=300)
    mgr = _mgr(_policy(reject_on_stale_snapshot=True), {"DU1": snap})
    handler = _handler()
    with pytest.raises(ValueError, match="MARGIN_SNAPSHOT_STALE"):
        await mgr._fanout_single_account(_signal(), handler, _acct("DU1"), raise_if_single=True)
    handler.build_intent.assert_awaited()

    mgr2 = _mgr(_policy(reject_on_stale_snapshot=False, min_free_pct_of_netliq=Decimal(0)), {"DU1": snap})
    handler2 = _handler()
    mgr2._evaluate_and_submit = AsyncMock(
        return_value=ExecutionResult(
            order=MagicMock(),
            rms_result=RMSResult(
                outcome=RMSOutcome.PASS,
                intent=handler2.build_intent.return_value,
                original_intent=handler2.build_intent.return_value,
                check_results=[],
            ),
            success=True,
        )
    )
    await mgr2._fanout_single_account(_signal(), handler2, _acct("DU1"), raise_if_single=True)
    handler2.build_intent.assert_awaited()


@pytest.mark.asyncio
async def test_close_and_flatten_skip_gate() -> None:
    mgr = _mgr(_policy(), {})
    handler = _handler()
    mgr._evaluate_and_submit = AsyncMock(
        return_value=ExecutionResult(
            order=MagicMock(),
            rms_result=RMSResult(
                outcome=RMSOutcome.PASS,
                intent=handler.build_intent.return_value,
                original_intent=handler.build_intent.return_value,
                check_results=[],
            ),
            success=True,
        )
    )
    close = await mgr._fanout_single_account(
        _signal(action="CLOSE"), handler, _acct("DU1"), raise_if_single=True
    )
    handler.build_intent.assert_awaited()
    assert close.error is None

    flatten_intent = OrderIntent(
        signal_id="F1",
        strategy_id="MODEL_BLUE",
        action=OrderAction.CLOSE,
        intent_mode=ExecutionIntentMode.EMERGENCY_FLATTEN,
        ibkr_account="DU1",
        legs=[
            OrderLeg(
                symbol="AAPL",
                side=OrderSide.SELL,
                quantity=1,
                price=Decimal(100),
                contract_month="2026-09",
            )
        ],
    )
    handler.build_intent = AsyncMock(return_value=flatten_intent)
    flatten = await mgr._fanout_single_account(
        _signal(action="CLOSE"), handler, _acct("DU1"), raise_if_single=True
    )
    assert flatten.error is None


@pytest.mark.asyncio
async def test_one_starved_sibling_does_not_block_healthy() -> None:
    policy = _policy()
    snapshots = {
        "DUSTARVE": _snap(ibkr_account="DUSTARVE", available_funds=Decimal(10)),
        "DUOK": _snap(ibkr_account="DUOK", available_funds=Decimal(50000)),
    }
    mgr = _mgr(policy, snapshots)
    built: list[str] = []

    async def _build(signal, account=None):
        built.append(account.ibkr_account)
        return OrderIntent(
            signal_id="SIG-G",
            strategy_id="MODEL_BLUE",
            action=OrderAction.OPEN,
            ibkr_account=account.ibkr_account,
            legs=[
                OrderLeg(
                    symbol="AAPL",
                    side=OrderSide.BUY,
                    quantity=1,
                    price=Decimal(100),
                    contract_month="2026-09",
                )
            ],
        )

    handler = MagicMock()
    handler.build_intent = AsyncMock(side_effect=_build)
    dummy = ExecutionResult(
        order=MagicMock(),
        rms_result=RMSResult(
            outcome=RMSOutcome.PASS,
            intent=OrderIntent(
                signal_id="x",
                strategy_id="MODEL_BLUE",
                action=OrderAction.OPEN,
                legs=[],
            ),
            original_intent=OrderIntent(
                signal_id="x",
                strategy_id="MODEL_BLUE",
                action=OrderAction.OPEN,
                legs=[],
            ),
            check_results=[],
        ),
        success=True,
    )
    async def _eval(intent: OrderIntent, *args: object, **kwargs: object) -> ExecutionResult:
        if getattr(intent, "ibkr_account", None) == "DUSTARVE":
            raise ValueError("MARGIN_NO_FREE_FUNDS: account=DUSTARVE")
        return dummy

    mgr._evaluate_and_submit = AsyncMock(side_effect=_eval)
    fanout = await mgr._fanout_accounts(
        _signal(),
        handler,
        [_acct("DUSTARVE", 1), _acct("DUOK", 2)],
    )
    assert "DUOK" in built
    by_acct = {o.ibkr_account: o for o in fanout.outcomes}
    assert by_acct["DUSTARVE"].error is not None
    assert "MARGIN_NO_FREE_FUNDS" in by_acct["DUSTARVE"].error
    assert by_acct["DUOK"].error is None
    assert by_acct["DUOK"].result is dummy
    assert fanout.had_unexpected_error is False
