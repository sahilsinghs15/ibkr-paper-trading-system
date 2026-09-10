"""Unit tests for ExecutionSnapshotCollector."""

import threading
from types import SimpleNamespace

from app.broker.ibkr.executions import (
    BrokerExecutionLine,
    ExecutionSnapshotCollector,
    normalize_execution_side,
    parse_ibkr_execution_time,
)


def test_normalize_execution_side() -> None:
    assert normalize_execution_side("BOT") == "BUY"
    assert normalize_execution_side("bot") == "BUY"
    assert normalize_execution_side("BUY") == "BUY"
    assert normalize_execution_side("SLD") == "SELL"
    assert normalize_execution_side("sld") == "SELL"
    assert normalize_execution_side("SELL") == "SELL"
    assert normalize_execution_side("SSHORT") == "SSHORT"
    assert normalize_execution_side(None) == "UNKNOWN"
    assert normalize_execution_side("") == "UNKNOWN"


def test_parse_ibkr_execution_time() -> None:
    # Standard format: YYYYMMDD HH:MM:SS
    iso = parse_ibkr_execution_time("20260910 14:30:15")
    assert "2026-09-10T14:30:15" in iso

    # With hyphen: YYYYMMDD-HH:MM:SS
    iso2 = parse_ibkr_execution_time("20260910-14:30:15")
    assert "2026-09-10T14:30:15" in iso2

    # None or empty
    fallback = parse_ibkr_execution_time(None)
    assert len(fallback) > 0


def test_collector_basic_lifecycle() -> None:
    collector = ExecutionSnapshotCollector()
    collector.reset(req_id=91001)

    contract = SimpleNamespace(
        symbol="AAPL",
        secType="STK",
        currency="USD",
        exchange="SMART",
        conId=12345,
    )
    execution = SimpleNamespace(
        execId="0001.exec.test",
        time="20260910 10:00:00",
        acctNumber="DU123456",
        shares=100.0,
        price=150.25,
        cumQty=100.0,
        avgPrice=150.25,
        side="BOT",
        orderId=5001,
        permId=99999,
        clientId=0,
    )

    collector.on_exec_details(91001, contract, execution)

    report = SimpleNamespace(
        execId="0001.exec.test",
        commission=1.05,
        currency="USD",
        realizedPNL=25.50,
    )
    collector.on_commission_report(report)

    assert not collector.wait(timeout=0.01)
    collector.on_exec_details_end(91001)
    assert collector.wait(timeout=0.1)

    lines = collector.snapshot()
    assert len(lines) == 1
    line = lines[0]
    assert isinstance(line, BrokerExecutionLine)
    assert line.exec_id == "0001.exec.test"
    assert line.ibkr_account == "DU123456"
    assert line.symbol == "AAPL"
    assert line.sec_type == "STK"
    assert line.side == "BUY"
    assert line.quantity == 100.0
    assert line.price == 150.25
    assert line.commission == 1.05
    assert line.commission_currency == "USD"
    assert line.realized_pnl == 25.50
    assert line.con_id == 12345
    assert line.broker_order_id == 5001


def test_collector_ignores_other_req_ids() -> None:
    collector = ExecutionSnapshotCollector()
    collector.reset(req_id=91002)

    contract = SimpleNamespace(symbol="MSFT", secType="STK", currency="USD", exchange="SMART", conId=222)
    exec1 = SimpleNamespace(execId="exec.wrong.1", acctNumber="DU123456", shares=10.0, price=300.0, side="BOT")
    exec2 = SimpleNamespace(execId="exec.right.2", acctNumber="DU123456", shares=20.0, price=305.0, side="SLD")

    # Unsolicited reqId = -1
    collector.on_exec_details(-1, contract, exec1)
    # Recovery reqId = 9003
    collector.on_exec_details(9003, contract, exec1)
    # Other reqId = 91001
    collector.on_exec_details(91001, contract, exec1)

    # Correct reqId = 91002
    collector.on_exec_details(91002, contract, exec2)

    # Wrong execDetailsEnd should not complete wait
    collector.on_exec_details_end(9003)
    assert not collector.wait(timeout=0.01)

    # Matching execDetailsEnd
    collector.on_exec_details_end(91002)
    assert collector.wait(timeout=0.1)

    lines = collector.snapshot()
    assert len(lines) == 1
    assert lines[0].exec_id == "exec.right.2"
    assert lines[0].side == "SELL"


def test_collector_retains_unknown_and_manual_orders() -> None:
    """Unknown orderId (None, 0, or missing) must be retained."""
    collector = ExecutionSnapshotCollector()
    collector.reset(req_id=91003)

    contract = SimpleNamespace(symbol="TSLA", secType="STK", currency="USD", exchange="ISLAND", conId=333)
    exec_unknown = SimpleNamespace(
        execId="exec.manual.tsla",
        acctNumber="DU123456",
        shares=50.0,
        price=200.0,
        side="BOT",
        orderId=None,
        permId=None,
        clientId=None,
    )

    collector.on_exec_details(91003, contract, exec_unknown)
    collector.on_exec_details_end(91003)

    lines = collector.snapshot()
    assert len(lines) == 1
    assert lines[0].exec_id == "exec.manual.tsla"
    assert lines[0].broker_order_id is None
    assert lines[0].perm_id is None
    assert lines[0].client_id is None


def test_collector_commission_arriving_before_exec_details() -> None:
    """Commission report arriving out of order before execDetails must be buffered and attached."""
    collector = ExecutionSnapshotCollector()
    collector.reset(req_id=91004)

    report = SimpleNamespace(
        execId="exec.out.of.order",
        commission=2.50,
        currency="USD",
        realizedPNL=100.0,
    )
    collector.on_commission_report(report)

    contract = SimpleNamespace(symbol="NVDA", secType="STK", currency="USD", exchange="SMART", conId=444)
    execution = SimpleNamespace(
        execId="exec.out.of.order",
        acctNumber="DU123456",
        shares=15.0,
        price=120.0,
        side="BOT",
    )
    collector.on_exec_details(91004, contract, execution)
    collector.on_exec_details_end(91004)

    lines = collector.snapshot()
    assert len(lines) == 1
    assert lines[0].commission == 2.50
    assert lines[0].commission_currency == "USD"
    assert lines[0].realized_pnl == 100.0


def test_collector_handles_duplicate_exec_id_and_corrections() -> None:
    """Duplicate execDetails are idempotent; corrections update existing record and retain commission."""
    collector = ExecutionSnapshotCollector()
    collector.reset(req_id=91005)

    contract = SimpleNamespace(symbol="AMZN", secType="STK", currency="USD", exchange="SMART", conId=555)
    exec_initial = SimpleNamespace(
        execId="exec.amzn.1",
        acctNumber="DU123456",
        shares=10.0,
        price=180.0,
        side="BOT",
    )
    collector.on_exec_details(91005, contract, exec_initial)

    # Attach commission
    report = SimpleNamespace(execId="exec.amzn.1", commission=1.0, currency="USD", realizedPNL=0.0)
    collector.on_commission_report(report)

    # Duplicate delivery with same shares/price
    collector.on_exec_details(91005, contract, exec_initial)
    lines = collector.snapshot()
    assert len(lines) == 1
    assert lines[0].quantity == 10.0
    assert lines[0].commission == 1.0

    # Trade correction from IBKR: price revised to 180.50
    exec_corrected = SimpleNamespace(
        execId="exec.amzn.1",
        acctNumber="DU123456",
        shares=10.0,
        price=180.50,
        side="BOT",
    )
    collector.on_exec_details(91005, contract, exec_corrected)

    lines = collector.snapshot()
    assert len(lines) == 1
    assert lines[0].price == 180.50
    # Retained previously attached commission
    assert lines[0].commission == 1.0


def test_collector_handles_unset_dbl_max_realized_pnl() -> None:
    """IBKR sends DBL_MAX (~1.79e308) when realized PnL is unset; should become None."""
    collector = ExecutionSnapshotCollector()
    collector.reset(req_id=91006)

    contract = SimpleNamespace(symbol="AMD", secType="STK", currency="USD", exchange="SMART", conId=666)
    execution = SimpleNamespace(execId="exec.amd.1", acctNumber="DU123456", shares=5.0, price=140.0, side="BOT")
    collector.on_exec_details(91006, contract, execution)

    report = SimpleNamespace(
        execId="exec.amd.1",
        commission=0.50,
        currency="USD",
        realizedPNL=1.7976931348623157e308,
    )
    collector.on_commission_report(report)

    lines = collector.snapshot()
    assert len(lines) == 1
    assert lines[0].commission == 0.50
    assert lines[0].realized_pnl is None


def test_collector_thread_safety() -> None:
    """Concurrent execution and commission reports must not corrupt collector state."""
    collector = ExecutionSnapshotCollector()
    collector.reset(req_id=91007)

    def worker(start: int, count: int) -> None:
        for i in range(start, start + count):
            exec_id = f"exec.thread.{i}"
            contract = SimpleNamespace(symbol="SPY", secType="STK", currency="USD", exchange="SMART", conId=i)
            execution = SimpleNamespace(execId=exec_id, acctNumber="DU1", shares=1.0, price=500.0, side="BOT")
            collector.on_exec_details(91007, contract, execution)
            report = SimpleNamespace(execId=exec_id, commission=0.1, currency="USD", realizedPNL=1.0)
            collector.on_commission_report(report)

    threads = [threading.Thread(target=worker, args=(i * 20, 20)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    collector.on_exec_details_end(91007)
    assert collector.wait(timeout=0.1)
    lines = collector.snapshot()
    assert len(lines) == 100
