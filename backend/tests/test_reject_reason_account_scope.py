"""Regression: exception-path reject reasons are tagged with the IBKR account.

accounts.id is BIGINT and account_scope arrives as a string (ingest job scope).
Comparing the column to a str made PostgreSQL raise
"operator does not exist: bigint = character varying", so the reason fell back
to the bare numeric id and a traceback was logged on every failed execution.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.db.models.account import AccountModel
from app.services.model_blue.parser import parse_model_blue_payload
from app.services.order_manager import OrderManager


def _payload(trade_id: str) -> dict:
    return {
        "market": "SMART",
        "strategy": "model_blue",
        "action": "OPEN",
        "trade_id": trade_id,
        "direction": 1,
        "buckets": [
            {"underlying": "XLE", "legs": [{"instrument_type": "STK", "side": "BUY", "weight": 0.5, "price": 90.0}]},
            {"underlying": "XOP", "legs": [{"instrument_type": "STK", "side": "SELL", "weight": 0.5, "price": 130.0}]},
        ],
    }


@pytest.mark.asyncio
async def test_exception_path_tags_reject_reason_with_ibkr_account(session_factory, caplog):
    account_id = 60_000_000 + uuid.uuid4().int % 1_000_000_000
    ibkr = f"DURJ{uuid.uuid4().hex[:6].upper()}"
    async with session_factory() as s, s.begin():
        s.add(
            AccountModel(
                id=account_id,
                name="reject-scope",
                ibkr_account=ibkr,
                total_margin=Decimal(100000),
                enabled=True,
            )
        )

    manager = OrderManager(strategy_id="model_blue", session_factory=session_factory)
    signal = parse_model_blue_payload(
        _payload(f"MBG-RJ-{uuid.uuid4().hex[:6]}"), timestamp=datetime.now(UTC), reason="t"
    )
    persist = AsyncMock(return_value=None)
    clock = MagicMock()
    clock.projected_in_red_zone.return_value = False

    with (
        patch.object(manager, "_persist_inbound_signal", persist),
        patch.object(
            manager,
            "_process_signal_execution_inner",
            AsyncMock(side_effect=RuntimeError("broker exploded")),
        ),
        patch("app.services.session_clock.get_session_clock", return_value=clock),
        caplog.at_level(logging.ERROR, logger="app.services.order_manager"),
        pytest.raises(RuntimeError, match="broker exploded"),
    ):
        await manager.process_signal_execution(signal, account_scope=str(account_id))

    reasons = [c.kwargs.get("reject_reason") for c in persist.call_args_list]
    assert f"Account {ibkr}: broker exploded" in reasons
    assert not any("Failed resolving account_scope" in r.getMessage() for r in caplog.records)
