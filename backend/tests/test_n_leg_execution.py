"""Architecture tests: generic N-leg RMS/OMS/IBKR vs Model Blue strategy isolation."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.broker.ibkr.tws_client import TWSClient
from app.models.signal import Signal, SignalType
from app.oms.ibkr_adapter import IBKRExecutionAdapter
from app.oms.models import OMSOrderStatus
from app.oms.oms_service import OMSService
from app.rms.checks.base import BaseRMSCheck
from app.rms.engine import RMSEngine
from app.rms.models import (
    CheckResult,
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
    RMSContext,
    RMSOutcome,
    StrategyConfig,
)
from app.services.model_blue.allocation import TemporarySettingsCommittedCapitalProvider
from app.services.model_blue.parser import parse_model_blue_payload
from app.services.model_blue.sizer import ModelBlueSizer
from app.services.order_manager import OrderManager
from tests.ibkr_test_utils import DEFAULT_TEST_IBKR_ACCOUNT, fill_on_place_order, wire_test_managed_accounts
from app.services.strategies.inbound import parse_tradingview_payload

_TS = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
_SYNTH = "synthetic_n_leg"


class _LegRecorder(BaseRMSCheck):
    def __init__(self) -> None:
        self.symbols: list[str] = []

    @property
    def check_number(self) -> int:
        return 99

    @property
    def check_name(self) -> str:
        return "LEG_RECORDER"

    def evaluate(self, intent: OrderIntent, context: RMSContext) -> CheckResult:
        self.symbols = [leg.symbol for leg in intent.legs]
        return CheckResult(
            check_number=self.check_number,
            check_name=self.check_name,
            outcome=RMSOutcome.PASS,
        )


def _leg(symbol: str, index: int, notional: Decimal = Decimal(200)) -> OrderLeg:
    price = Decimal(10)
    qty = float(notional / price)
    return OrderLeg(
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=qty,
        price=price,
        contract_month="2026-09",
        notional=notional,
        instrument_type="STK",
        leg_index=index,
    )


def _intent(symbols: list[str], *, signal_id: str) -> OrderIntent:
    return OrderIntent(
        signal_id=signal_id,
        strategy_id=_SYNTH,
        action=OrderAction.OPEN,
        ibkr_account=DEFAULT_TEST_IBKR_ACCOUNT,
        legs=[_leg(sym, i) for i, sym in enumerate(symbols)],
        timestamp=_TS,
    )


def _rms_context() -> RMSContext:
    return RMSContext(
        strategy_configs={
            _SYNTH: StrategyConfig(
                strategy_id=_SYNTH,
                max_open_positions=10,
                money_limit_per_symbol=Decimal(1_000_000),
            )
        }
    )


def _mock_adapter() -> IBKRExecutionAdapter:
    client = MagicMock(spec=TWSClient)
    client.is_connected.return_value = True
    client.next_order_id = 200
    client.get_request_type.return_value = "order"
    adapter = IBKRExecutionAdapter(client=client)
    wire_test_managed_accounts(adapter)
    fill_on_place_order(adapter, client)
    return adapter


async def _run_pipeline(intent: OrderIntent) -> tuple[Any, IBKRExecutionAdapter, list]:
    adapter = _mock_adapter()
    submit_calls: list[Any] = []
    original_submit = adapter.submit_order

    async def spy_submit(order: Any) -> Any:
        submit_calls.append(order)
        return await original_submit(order)

    adapter.submit_order = spy_submit  # type: ignore[method-assign]
    recorder = _LegRecorder()
    engine = RMSEngine(checks=[recorder, *list(RMSEngine().checks)])
    rms_result = engine.evaluate(intent, _rms_context())
    assert rms_result.outcome == RMSOutcome.PASS
    assert recorder.symbols == [leg.symbol for leg in intent.legs]
    oms = OMSService(adapter=adapter)
    exec_res = await oms.submit_intent(intent=intent, rms_result=rms_result)
    return exec_res, adapter, submit_calls


def test_1_two_leg_model_blue_intent_unchanged() -> None:
    payload = {
        "market": "SMART",
        "strategy": "model_blue",
        "action": "OPEN",
        "trade_id": "MBG-ARCH-XLE-XOP",
        "direction": 1,
        "buckets": [
            {
                "underlying": "XLE",
                "legs": [{"instrument_type": "STK", "side": "BUY", "weight": 0.5943, "price": 62.59}],
            },
            {
                "underlying": "XOP",
                "legs": [{"instrument_type": "STK", "side": "SELL", "weight": -0.4057, "price": 183.34}],
            },
        ],
    }
    signal = parse_model_blue_payload(payload, timestamp=_TS, reason="arch")
    sizer = ModelBlueSizer(TemporarySettingsCommittedCapitalProvider(Decimal(25000)))
    sized = sizer.size_open(signal)
    assert len(sized) == 2
    assert sized[0].symbol == "XLE"
    assert sized[1].symbol == "XOP"
    assert sized[0].side == OrderSide.BUY
    assert sized[1].side == OrderSide.SELL


def test_rms_rejects_when_third_leg_breaches_money_limit() -> None:
    """If RMS ignored legs beyond the second, this intent would PASS."""
    intent = OrderIntent(
        signal_id="THREE-BREACH",
        strategy_id=_SYNTH,
        action=OrderAction.OPEN,
        legs=[
            _leg("AAA", 0, Decimal(100)),
            _leg("BBB", 1, Decimal(100)),
            _leg("CCC", 2, Decimal(9000)),
        ],
        timestamp=_TS,
    )
    context = RMSContext(
        strategy_configs={
            _SYNTH: StrategyConfig(
                strategy_id=_SYNTH,
                max_open_positions=10,
                money_limit_per_symbol=Decimal(5000),
            )
        }
    )
    result = RMSEngine().evaluate(intent, context)
    assert result.outcome == RMSOutcome.REJECT
    assert result.reason is not None and "CCC" in result.reason


@pytest.mark.asyncio
async def test_2_generic_three_leg_intent() -> None:
    intent = _intent(["L0", "L1", "L2"], signal_id="N3")
    exec_res, adapter, submit_calls = await _run_pipeline(intent)
    assert exec_res.success is True
    assert len(exec_res.orders) == 3
    assert [o.symbol for o in exec_res.orders] == ["L0", "L1", "L2"]
    assert len(submit_calls) == 3
    assert adapter._client.placeOrder.call_count == 3


@pytest.mark.asyncio
async def test_3_generic_five_leg_intent() -> None:
    intent = _intent(["A", "B", "C", "D", "E"], signal_id="N5")
    exec_res, adapter, submit_calls = await _run_pipeline(intent)
    assert exec_res.success is True
    assert len(exec_res.orders) == 5
    assert [o.symbol for o in exec_res.orders] == ["A", "B", "C", "D", "E"]
    assert len(submit_calls) == 5
    assert adapter._client.placeOrder.call_count == 5


@pytest.mark.asyncio
async def test_4_strategy_isolation_skips_model_blue_sizer() -> None:
    sizer = MagicMock(spec=ModelBlueSizer)
    adapter = _mock_adapter()
    oms = OMSService(adapter=adapter)
    order_manager = OrderManager(
        oms=oms,
        symbol=None,
        quantity=None,
        order_type="MARKET",
        model_blue_sizer=sizer,
        committed_capital_provider=TemporarySettingsCommittedCapitalProvider(Decimal(25000)),
        rms_context=_rms_context(),
        rms_engine=RMSEngine(),
    )
    from dataclasses import replace

    original_eval = order_manager._evaluate_and_submit

    async def eval_with_test_account(intent, *args, **kwargs):
        if not intent.ibkr_account:
            intent = replace(intent, ibkr_account=DEFAULT_TEST_IBKR_ACCOUNT)
        return await original_eval(intent, *args, **kwargs)

    order_manager._evaluate_and_submit = eval_with_test_account  # type: ignore[method-assign]
    signal = Signal(
        signal_type=SignalType.BUY,
        timestamp=_TS,
        reason="other strategy",
        signal_id="RED-1",
        strategy_id="model_red",
        action="OPEN",
        symbol="SPY",
        side="BUY",
        price=Decimal(100),
        quantity=1,
    )
    with patch("app.services.model_blue.parser.parse_model_blue_payload") as parse_spy:
        result = await order_manager.process_signal_execution(signal)
        parse_spy.assert_not_called()
    sizer.size_open.assert_not_called()
    assert result is not None
    assert len(result.orders) == 1
    assert result.orders[0].symbol == "SPY"
    assert result.orders[0].status == OMSOrderStatus.FILLED

    inbound = parse_tradingview_payload(
        {"strategy": "model_red", "symbol": "SPY", "quantity": 1, "price": 100, "action": "OPEN"},
        timestamp=_TS,
        request_id="req-iso",
        capture_data={},
    )
    assert inbound.strategy_id == "model_red"
    assert inbound.legs == ()
    assert inbound.quantity == 1
