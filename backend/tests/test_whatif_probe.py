"""IBKR what-if probe: whatIf flag, separate pending map, inf, cancelOrder."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ibapi.contract import Contract

from app.core.config import Settings
from app.instruments.models import ResolvedInstrument
from app.oms.ibkr_adapter import IBKRExecutionAdapter, WhatIfResult
from app.rms.models import (
    MarginPolicy,
    OrderAction,
    OrderIntent,
    OrderLeg,
    OrderSide,
    RMSContext,
)
from app.services.account_margin import AccountMarginSnapshot
from app.services.order_manager import OrderManager


def _enabled_settings(**kwargs) -> Settings:
    s = Settings(_env_file=None)
    s.margin_whatif_enabled = True
    s.margin_whatif_timeout_sec = 2.0
    for key, value in kwargs.items():
        setattr(s, key, value)
    return s


def _adapter() -> tuple[IBKRExecutionAdapter, MagicMock]:
    tws = MagicMock()
    tws.next_order_id = 500
    tws.is_connected.return_value = True
    adapter = IBKRExecutionAdapter(client=tws)
    adapter.is_connected = lambda: True  # type: ignore[method-assign]
    return adapter, tws


def _contract() -> Contract:
    c = Contract()
    c.symbol = "AAPL"
    c.secType = "STK"
    c.exchange = "SMART"
    c.currency = "USD"
    return c


@pytest.mark.asyncio
async def test_whatif_flag_true_and_id_not_in_live_orders() -> None:
    adapter, tws = _adapter()
    captured: dict[str, object] = {}

    def _place(oid, contract, ib_order):
        captured["whatIf"] = ib_order.whatIf
        captured["oid"] = oid
        state = MagicMock()
        state.initMarginChange = "25.5"
        state.maintMarginChange = "10"
        state.initMarginAfter = "100"
        state.commission = "1"
        state.warningText = ""
        adapter.on_open_order(oid, contract, ib_order, state)

    tws.placeOrder.side_effect = _place
    with patch("app.oms.ibkr_adapter.get_settings", return_value=_enabled_settings()):
        result = await adapter.probe_margin(
            contract=_contract(),
            side="BUY",
            quantity=Decimal(10),
            price=Decimal(100),
            ibkr_account="DU1",
        )
    assert captured["whatIf"] is True
    assert captured["oid"] == 500
    assert 500 not in adapter._orders_by_tws_id
    assert result.unknown is False
    assert result.init_margin_change == Decimal("25.5")
    tws.cancelOrder.assert_called_once_with(500)


@pytest.mark.asyncio
async def test_inf_is_unknown() -> None:
    adapter, tws = _adapter()

    def _place(oid, contract, ib_order):
        state = MagicMock()
        state.initMarginChange = "inf"
        adapter.on_open_order(oid, contract, ib_order, state)

    tws.placeOrder.side_effect = _place
    with patch("app.oms.ibkr_adapter.get_settings", return_value=_enabled_settings()):
        result = await adapter.probe_margin(
            contract=_contract(),
            side="BUY",
            quantity=1,
            price=Decimal(100),
            ibkr_account="DU1",
        )
    assert result.unknown is True
    assert result.init_margin_change is None


@pytest.mark.asyncio
async def test_timeout_is_unknown_not_hang() -> None:
    adapter, tws = _adapter()
    tws.placeOrder.side_effect = lambda *a, **k: None
    settings = _enabled_settings(margin_whatif_timeout_sec=0.05)
    with patch("app.oms.ibkr_adapter.get_settings", return_value=settings):
        result = await adapter.probe_margin(
            contract=_contract(),
            side="BUY",
            quantity=1,
            price=Decimal(100),
            ibkr_account="DU1",
        )
    assert result.unknown is True
    tws.cancelOrder.assert_called()


@pytest.mark.asyncio
async def test_disabled_never_calls_place_order() -> None:
    adapter, tws = _adapter()
    settings = Settings(_env_file=None)
    settings.margin_whatif_enabled = False
    with patch("app.oms.ibkr_adapter.get_settings", return_value=settings):
        result = await adapter.probe_margin(
            contract=_contract(),
            side="BUY",
            quantity=1,
            price=Decimal(100),
            ibkr_account="DU1",
        )
    assert result.unknown is True
    tws.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_borderline_unknown_probe_raises_not_fallback() -> None:
    adapter = MagicMock()
    adapter.probe_margin = AsyncMock(return_value=WhatIfResult(order_id=1, unknown=True))
    oms = MagicMock()
    oms._adapter = adapter
    now = datetime.now(UTC)
    context = RMSContext(
        margin_policy=MarginPolicy(
            check_enabled=True,
            confirm_borderline=True,
            min_free_buffer=Decimal(0),
            min_free_pct_of_netliq=Decimal(0),
            comfort_ratio=Decimal("0.80"),
            default_rate=Decimal("0.85"),
            rate_safety_multiplier=Decimal(1),
        ),
        margin_snapshots={
            "DU1": AccountMarginSnapshot(
                ibkr_account="DU1",
                as_of=now,
                available_funds=Decimal(1000),
                max_age_sec=300,
            )
        },
    )
    mgr = OrderManager(oms=oms, rms_context=context)
    resolved = ResolvedInstrument(
        symbol="AAPL",
        requested_instrument_type="STK",
        sec_type="STK",
        exchange="SMART",
        currency="USD",
        con_id=1,
    )
    # required = 1000 * 0.85 = 850; usable=1000; comfort=800 → BORDERLINE
    intent = OrderIntent(
        signal_id="B1",
        strategy_id="MODEL_BLUE",
        action=OrderAction.OPEN,
        ibkr_account="DU1",
        legs=[
            OrderLeg(
                symbol="AAPL",
                side=OrderSide.BUY,
                quantity=10,
                price=Decimal(100),
                contract_month="2026-09",
                resolved=resolved,
            )
        ],
    )
    with pytest.raises(ValueError, match="MARGIN_PROBE_UNKNOWN"):
        await mgr._confirm_margin_if_borderline(intent)
