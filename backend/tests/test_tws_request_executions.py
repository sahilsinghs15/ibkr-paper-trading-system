from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.broker.ibkr.gateway_rate_limiter import (
    PRIORITY_DIAGNOSTIC,
    GatewayRateLimiter,
)
from app.broker.ibkr.tws_client import TWSClient


def test_request_executions_disconnected_returns_empty() -> None:
    client = TWSClient()
    with patch.object(client, "is_connected", return_value=False):
        lines, timed_out = client.request_executions(ibkr_account="DU123456", timeout=1.0)
        assert lines == []
        assert timed_out is False


def test_request_executions_success_flow() -> None:
    client = TWSClient()

    with (
        patch.object(client, "is_connected", return_value=True),
        patch.object(client, "reqExecutions") as mock_req,
    ):
        def fire_executions(req_id: int, execution_filter: object) -> None:
            assert getattr(execution_filter, "acctCode", "") == "DU123456"
            assert getattr(execution_filter, "clientId", -1) == 0
            assert req_id >= 91000
            assert req_id != 9003

            contract = SimpleNamespace(symbol="NVDA", secType="STK", currency="USD", exchange="SMART", conId=101)
            execution = SimpleNamespace(
                execId="exec.nvda.1",
                time="20260910 11:00:00",
                acctNumber="DU123456",
                shares=25.0,
                price=118.5,
                cumQty=25.0,
                avgPrice=118.5,
                side="BOT",
                orderId=8001,
                permId=123456,
                clientId=0,
            )
            # Fire execDetails, commissionReport, execDetailsEnd
            client.execDetails(req_id, contract, execution)
            report = SimpleNamespace(execId="exec.nvda.1", commission=1.0, currency="USD", realizedPNL=0.0)
            client.commissionReport(report)
            client.execDetailsEnd(req_id)

        mock_req.side_effect = fire_executions

        lines, timed_out = client.request_executions(ibkr_account="DU123456", timeout=1.0)

    assert timed_out is False
    assert len(lines) == 1
    assert lines[0].symbol == "NVDA"
    assert lines[0].quantity == 25.0
    assert lines[0].side == "BUY"
    assert lines[0].commission == 1.0


def test_request_executions_uses_priority_diagnostic() -> None:
    client = TWSClient()
    limiter = MagicMock(spec=GatewayRateLimiter)
    limiter.max_wait_sec = 8.0
    limiter.blocking_acquire.return_value = MagicMock()
    client.register_rate_limiter(limiter)

    with (
        patch.object(client, "is_connected", return_value=True),
        patch.object(client, "reqExecutions") as mock_req,
    ):
        def fire_end(req_id: int, _filter: object) -> None:
            client.execDetailsEnd(req_id)

        mock_req.side_effect = fire_end
        _lines, timed_out = client.request_executions(ibkr_account="DU123456", timeout=1.0)

    limiter.blocking_acquire.assert_called_once()
    call_args = limiter.blocking_acquire.call_args
    assert call_args[0][0] == PRIORITY_DIAGNOSTIC
    assert call_args[0][1] == "reqExecutions"
    assert timed_out is False


def test_request_executions_isolation_from_unsolicited_and_recovery() -> None:
    client = TWSClient()
    mock_listener = MagicMock()
    client.register_listener(mock_listener)

    with (
        patch.object(client, "is_connected", return_value=True),
        patch.object(client, "reqExecutions") as mock_req,
    ):
        def fire_mixed(req_id: int, _filter: object) -> None:
            contract = SimpleNamespace(symbol="AAPL", secType="STK", currency="USD", exchange="SMART", conId=1)
            # 1. Unsolicited live fill (reqId == -1)
            unsolicited = SimpleNamespace(execId="unsolicited.1", acctNumber="DU1", shares=10.0, price=150.0, side="BOT")
            client.execDetails(-1, contract, unsolicited)

            # 2. Recovery fill (reqId == 9003)
            recovery = SimpleNamespace(execId="recovery.1", acctNumber="DU1", shares=20.0, price=150.0, side="BOT")
            client.execDetails(9003, contract, recovery)
            client.execDetailsEnd(9003)

            # 3. Snapshot request fill (req_id == 91000+)
            snapshot_exec = SimpleNamespace(execId="snap.1", acctNumber="DU1", shares=30.0, price=150.0, side="SLD")
            client.execDetails(req_id, contract, snapshot_exec)
            client.execDetailsEnd(req_id)

        mock_req.side_effect = fire_mixed
        lines, timed_out = client.request_executions(ibkr_account="DU1", timeout=1.0)

    # The Trade Book snapshot should ONLY have the snapshot execution
    assert timed_out is False
    assert len(lines) == 1
    assert lines[0].exec_id == "snap.1"
    assert lines[0].quantity == 30.0

    # The registered listener (e.g. IBKRExecutionAdapter) received unsolicited and recovery, NOT snapshot!
    listener_calls = [call[0][0] for call in mock_listener.on_exec_details.call_args_list]
    assert -1 in listener_calls
    assert 9003 in listener_calls
    for call in mock_listener.on_exec_details.call_args_list:
        assert call[0][0] != lines[0].exec_id
        assert call[0][0] < 91000  # snapshot reqId was NOT passed to listener


def test_request_executions_timeout() -> None:
    client = TWSClient()

    with (
        patch.object(client, "is_connected", return_value=True),
        patch.object(client, "reqExecutions") as mock_req,
    ):
        def fire_no_end(req_id: int, _filter: object) -> None:
            contract = SimpleNamespace(symbol="TSLA", secType="STK", currency="USD", exchange="SMART", conId=2)
            execution = SimpleNamespace(execId="exec.partial.1", acctNumber="DU1", shares=5.0, price=200.0, side="BOT")
            client.execDetails(req_id, contract, execution)
            # Does NOT call execDetailsEnd

        mock_req.side_effect = fire_no_end
        lines, timed_out = client.request_executions(ibkr_account="DU1", timeout=0.05)

    assert timed_out is True
    assert len(lines) == 1
    assert lines[0].exec_id == "exec.partial.1"


@pytest.mark.asyncio
async def test_request_executions_async() -> None:
    client = TWSClient()

    with (
        patch.object(client, "is_connected", return_value=True),
        patch.object(client, "reqExecutions") as mock_req,
    ):
        def fire(req_id: int, _filter: object) -> None:
            contract = SimpleNamespace(symbol="GOOGL", secType="STK", currency="USD", exchange="SMART", conId=3)
            execution = SimpleNamespace(execId="exec.googl.1", acctNumber="DU1", shares=8.0, price=170.0, side="BOT")
            client.execDetails(req_id, contract, execution)
            client.execDetailsEnd(req_id)

        mock_req.side_effect = fire
        lines, timed_out = await client.request_executions_async(ibkr_account="DU1", timeout=1.0)

    assert timed_out is False
    assert len(lines) == 1
    assert lines[0].symbol == "GOOGL"
