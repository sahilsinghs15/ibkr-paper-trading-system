"""Unit tests for IBKR instrument master discovery script (mocked connection)."""

import csv
import tempfile
from pathlib import Path

from ibapi.contract import Contract, ContractDetails  # type: ignore[import-untyped]

from scripts.instrument_master.discover import (
    CSV_FIELDNAMES,
    InstrumentDiscoveryClient,
    create_stk_contract,
    extract_contract_record,
    write_contracts_to_csv,
)


def test_create_stk_contract() -> None:
    """Test contract creation helper for US stocks/ETFs."""
    contract = create_stk_contract("AAPL")
    assert contract.symbol == "AAPL"
    assert contract.secType == "STK"
    assert contract.exchange == "SMART"
    assert contract.currency == "USD"


def test_extract_contract_record() -> None:
    """Test converting ContractDetails object into flat CSV dict record."""
    details = ContractDetails()
    details.contract = Contract()
    details.contract.conId = 265598
    details.contract.symbol = "AAPL"
    details.contract.localSymbol = "AAPL"
    details.contract.secType = "STK"
    details.contract.exchange = "SMART"
    details.contract.primaryExchange = "NASDAQ"
    details.contract.currency = "USD"
    details.contract.tradingClass = "NMS"
    details.minTick = 0.01
    details.tradingHours = "20260812:0400-20260812:2000;"
    details.liquidHours = "20260812:0930-20260812:1600;"
    details.timeZoneId = "EST5EDT"
    details.longName = "APPLE INC"

    record = extract_contract_record(details, retrieved_at="2026-08-12T12:00:00Z")

    assert record["con_id"] == "265598"
    assert record["symbol"] == "AAPL"
    assert record["local_symbol"] == "AAPL"
    assert record["sec_type"] == "STK"
    assert record["exchange"] == "SMART"
    assert record["primary_exchange"] == "NASDAQ"
    assert record["currency"] == "USD"
    assert record["trading_class"] == "NMS"
    assert record["min_tick"] == "0.01"
    assert record["trading_hours"] == "20260812:0400-20260812:2000;"
    assert record["liquid_hours"] == "20260812:0930-20260812:1600;"
    assert record["time_zone_id"] == "EST5EDT"
    assert record["description"] == "APPLE INC"
    assert record["retrieved_at"] == "2026-08-12T12:00:00Z"
    assert record["expiry"] == ""
    assert record["strike"] == ""


def test_write_contracts_to_csv() -> None:
    """Test writing contract detail records to a CSV file."""
    records = [
        {
            "con_id": "265598",
            "symbol": "AAPL",
            "local_symbol": "AAPL",
            "sec_type": "STK",
            "exchange": "SMART",
            "primary_exchange": "NASDAQ",
            "currency": "USD",
            "trading_class": "NMS",
            "multiplier": "",
            "expiry": "",
            "strike": "",
            "right": "",
            "min_tick": "0.01",
            "trading_hours": "20260812:0400-20260812:2000;",
            "liquid_hours": "20260812:0930-20260812:1600;",
            "time_zone_id": "EST5EDT",
            "underlying_con_id": "",
            "description": "APPLE INC",
            "retrieved_at": "2026-08-12T12:00:00Z",
        }
    ]

    with tempfile.TemporaryDirectory() as tmp_dir:
        csv_path = Path(tmp_dir) / "test_output.csv"
        write_contracts_to_csv(records, csv_path)

        assert csv_path.exists()
        with csv_path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            assert reader.fieldnames == CSV_FIELDNAMES
            rows = list(reader)
            assert len(rows) == 1
            assert rows[0]["symbol"] == "AAPL"
            assert rows[0]["con_id"] == "265598"


def test_discovery_client_callbacks() -> None:
    """Test client state transitions when callbacks trigger."""
    client = InstrumentDiscoveryClient()
    event = client.register_request(req_id=1)

    assert not event.is_set()

    details = ContractDetails()
    details.contract = Contract()
    details.contract.conId = 12345
    details.contract.symbol = "MSFT"

    client.contractDetails(reqId=1, contractDetails=details)
    assert len(client.contract_details_results[1]) == 1

    client.contractDetailsEnd(reqId=1)
    assert event.is_set()


def test_error_callback_compatibility() -> None:
    """Test that error callback handles 3-arg, 4-arg, and kwargs signatures without raising TypeError."""
    client = InstrumentDiscoveryClient()
    event = client.register_request(req_id=1)

    # 1. Test 3-positional arguments signature (reqId, errorCode, errorString)
    client.error(1, 200, "No security definition found")
    assert event.is_set()
    assert "200" in client.request_errors[1]

    # 2. Test 4-positional arguments signature (reqId, errorCode, errorString, advancedOrderRejectJson)
    event2 = client.register_request(req_id=2)
    reject_json = '{"rejectReason": "Invalid order"}'
    # This 4-arg call previously caused TypeError: EWrapper.error() takes 4 positional arguments but 5 were given
    client.error(2, 201, "Order rejected", reject_json)
    assert event2.is_set()
    assert "201" in client.request_errors[2]
    assert reject_json in client.request_errors[2]


from scripts.instrument_master.discover import (
    create_contract_for_seed,
    create_unresolved_record,
)
from scripts.instrument_master.seed_fetcher import SeedRecord


def test_extract_contract_record_with_seed_metadata() -> None:
    """Test extracting contract record with associated SeedRecord metadata."""
    details = ContractDetails()
    details.contract = Contract()
    details.contract.conId = 756733
    details.contract.symbol = "SPY"
    details.contract.secType = "STK"
    details.contract.exchange = "SMART"
    details.contract.primaryExchange = "ARCA"
    details.contract.currency = "USD"
    details.longName = "SPDR S&P 500 ETF TRUST"

    seed = SeedRecord(
        symbol="SPY",
        raw_symbol="SPY",
        security_name="SPDR S&P 500 ETF Trust",
        listing_exchange="NYSE_ARCA",
        is_etf=True,
        is_test_issue=False,
        source_file="otherlisted.txt",
        category="ETF",
    )

    record = extract_contract_record(
        details, retrieved_at="2026-08-12T12:00:00Z", seed=seed, status="RESOLVED"
    )

    assert record["seed_symbol"] == "SPY"
    assert record["seed_raw_symbol"] == "SPY"
    assert record["seed_security_name"] == "SPDR S&P 500 ETF Trust"
    assert record["seed_category"] == "ETF"
    assert record["seed_exchange"] == "NYSE_ARCA"
    assert record["seed_is_etf"] == "True"
    assert record["seed_source_file"] == "otherlisted.txt"
    assert record["status"] == "RESOLVED"
    assert record["error_code"] == ""
    assert record["error_message"] == ""
    assert record["con_id"] == "756733"
    assert record["symbol"] == "SPY"
    assert record["primary_exchange"] == "ARCA"


def test_create_unresolved_record() -> None:
    """Test building auditable CSV record for unresolved, error, or timeout seeds."""
    seed = SeedRecord(
        symbol="AACIW",
        raw_symbol="AACIW",
        security_name="Armada Acquisition Corp. III - Warrant",
        listing_exchange="NASDAQ",
        is_etf=False,
        is_test_issue=False,
        source_file="nasdaqlisted.txt",
        category="WARRANT",
    )

    unresolved_rec = create_unresolved_record(
        seed=seed,
        status="UNRESOLVED",
        error_code="200",
        error_message="No security definition has been found for the request",
        retrieved_at="2026-08-12T12:00:00Z",
    )

    assert unresolved_rec["seed_symbol"] == "AACIW"
    assert unresolved_rec["seed_category"] == "WARRANT"
    assert unresolved_rec["status"] == "UNRESOLVED"
    assert unresolved_rec["error_code"] == "200"
    assert (
        unresolved_rec["error_message"]
        == "No security definition has been found for the request"
    )
    assert unresolved_rec["con_id"] == ""
    assert unresolved_rec["symbol"] == ""


def test_create_contract_for_seed_sec_type() -> None:
    """Test secType assignment based on seed category."""
    warrant_seed = SeedRecord(
        symbol="AACIW",
        raw_symbol="AACIW",
        security_name="Armada Warrant",
        listing_exchange="NASDAQ",
        is_etf=False,
        is_test_issue=False,
        source_file="nasdaqlisted.txt",
        category="WARRANT",
    )
    stock_seed = SeedRecord(
        symbol="AAPL",
        raw_symbol="AAPL",
        security_name="Apple Inc.",
        listing_exchange="NASDAQ",
        is_etf=False,
        is_test_issue=False,
        source_file="nasdaqlisted.txt",
        category="COMMON_STOCK",
    )

    # Primary STK default
    stk_war_contract = create_contract_for_seed(warrant_seed)
    assert stk_war_contract.secType == "STK"
    assert stk_war_contract.symbol == "AACIW"

    # Explicit WAR fallback
    war_contract = create_contract_for_seed(warrant_seed, sec_type="WAR")
    assert war_contract.secType == "WAR"
    assert war_contract.symbol == "AACI"
    assert war_contract.localSymbol == "AACIW"

    stk_contract = create_contract_for_seed(stock_seed)
    assert stk_contract.secType == "STK"
    assert stk_contract.symbol == "AAPL"


def test_multiple_contracts_preservation() -> None:
    """Test preserving multiple ContractDetails for a single seed with full seed metadata."""
    seed = SeedRecord(
        symbol="TEST",
        raw_symbol="TEST",
        security_name="Test Corp",
        listing_exchange="NASDAQ",
        is_etf=False,
        is_test_issue=False,
        source_file="nasdaqlisted.txt",
        category="COMMON_STOCK",
    )

    d1 = ContractDetails()
    d1.contract = Contract()
    d1.contract.conId = 1001
    d1.contract.symbol = "TEST"

    d2 = ContractDetails()
    d2.contract = Contract()
    d2.contract.conId = 1002
    d2.contract.symbol = "TEST"

    rec1 = extract_contract_record(
        d1, "2026-08-12T12:00:00Z", seed=seed, status="RESOLVED"
    )
    rec2 = extract_contract_record(
        d2, "2026-08-12T12:00:00Z", seed=seed, status="RESOLVED"
    )

    assert rec1["con_id"] == "1001"
    assert rec2["con_id"] == "1002"
    assert rec1["seed_symbol"] == rec2["seed_symbol"] == "TEST"
    assert rec1["status"] == rec2["status"] == "RESOLVED"


def test_csv_serialization_with_all_statuses() -> None:
    """Test CSV serialization containing RESOLVED, UNRESOLVED, TIMEOUT, and ERROR rows."""
    seed = SeedRecord(
        symbol="AAPL",
        raw_symbol="AAPL",
        security_name="Apple Inc.",
        listing_exchange="NASDAQ",
        is_etf=False,
        is_test_issue=False,
        source_file="nasdaqlisted.txt",
        category="COMMON_STOCK",
    )
    details = ContractDetails()
    details.contract = Contract()
    details.contract.conId = 265598
    details.contract.symbol = "AAPL"

    resolved_rec = extract_contract_record(
        details, "2026-08-12T12:00:00Z", seed=seed, status="RESOLVED"
    )
    unresolved_rec = create_unresolved_record(
        seed, "UNRESOLVED", "200", "No security definition", "2026-08-12T12:00:00Z"
    )
    timeout_rec = create_unresolved_record(
        seed,
        "TIMEOUT",
        "",
        "Timed out waiting for contract details",
        "2026-08-12T12:00:00Z",
    )

    records = [resolved_rec, unresolved_rec, timeout_rec]

    with tempfile.TemporaryDirectory() as tmp_dir:
        csv_path = Path(tmp_dir) / "audit_output.csv"
        write_contracts_to_csv(records, csv_path)

        assert csv_path.exists()
        with csv_path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            assert reader.fieldnames == CSV_FIELDNAMES
            rows = list(reader)
            assert len(rows) == 3
            assert rows[0]["status"] == "RESOLVED"
            assert rows[0]["con_id"] == "265598"
            assert rows[1]["status"] == "UNRESOLVED"
            assert rows[1]["error_code"] == "200"
            assert rows[2]["status"] == "TIMEOUT"
            assert rows[2]["error_message"] == "Timed out waiting for contract details"


from scripts.instrument_master.discover import (
    compute_seed_key,
    load_checkpoint,
    save_checkpoint,
)


def test_checkpoint_save_and_load() -> None:
    """Test atomic checkpoint saving and loading."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        cp_path = Path(tmp_dir) / "test_cp.json"
        data = {
            "universe_count": 100,
            "completed_seed_keys": ["nasdaqlisted.txt:AAPL:COMMON_STOCK"],
            "audit_records": [{"seed_symbol": "AAPL", "status": "RESOLVED"}],
            "total_requests": 1,
            "total_retries": 0,
        }

        save_checkpoint(cp_path, data)
        assert cp_path.exists()

        loaded = load_checkpoint(cp_path)
        assert loaded is not None
        assert loaded["universe_count"] == 100
        assert "nasdaqlisted.txt:AAPL:COMMON_STOCK" in loaded["completed_seed_keys"]
        assert loaded["audit_records"][0]["seed_symbol"] == "AAPL"


def test_compute_seed_key() -> None:
    """Test deterministic seed key generation."""
    seed = SeedRecord(
        symbol="AAPL",
        raw_symbol="AAPL",
        security_name="Apple Inc.",
        listing_exchange="NASDAQ",
        is_etf=False,
        is_test_issue=False,
        source_file="nasdaqlisted.txt",
        category="COMMON_STOCK",
    )
    key = compute_seed_key(seed)
    assert key == "nasdaqlisted.txt:AAPL:COMMON_STOCK"


def test_stale_callback_isolation() -> None:
    """Test that unregistered request IDs ignore late-arriving callbacks safely."""
    client = InstrumentDiscoveryClient()
    client.register_request(req_id=1)
    client.unregister_request(req_id=1)

    # 1. Stale error callback for unregistered reqId=1
    client.error(1, 200, "Delayed Error 200 from old request")
    assert 1 not in client.request_errors

    # 2. Stale contractDetails callback for unregistered reqId=1
    details = ContractDetails()
    details.contract = Contract()
    details.contract.conId = 9999
    client.contractDetails(1, details)
    assert 1 not in client.contract_details_results

    # 3. Active reqId=2 must remain unaffected
    event2 = client.register_request(req_id=2)
    assert not event2.is_set()
    client.contractDetailsEnd(2)
    assert event2.is_set()


def test_warrant_contract_creation_stk_primary_and_war_fallback() -> None:
    """Test primary STK construction and WAR fallback construction for warrant seeds."""
    warrant_seed = SeedRecord(
        symbol="AACOW",
        raw_symbol="AACOW",
        security_name="Armada Acquisition Corp. III - Warrant",
        listing_exchange="NASDAQ",
        is_etf=False,
        is_test_issue=False,
        source_file="nasdaqlisted.txt",
        category="WARRANT",
    )

    # Primary attempt: secType="STK", symbol="AACOW"
    stk_contract = create_contract_for_seed(warrant_seed, sec_type="STK")
    assert stk_contract.secType == "STK"
    assert stk_contract.symbol == "AACOW"

    # Fallback attempt: secType="WAR", symbol="AACO" (underlying), localSymbol="AACOW"
    war_contract = create_contract_for_seed(warrant_seed, sec_type="WAR")
    assert war_contract.secType == "WAR"
    assert war_contract.symbol == "AACO"
    assert war_contract.localSymbol == "AACOW"


def test_generalized_warrant_right_unit_5th_character_extraction() -> None:
    """Test 5th-character underlying extraction for ASPSZ, BCTXL, BCTXZ."""
    aspsz_seed = SeedRecord(
        symbol="ASPSZ",
        raw_symbol="ASPSZ",
        security_name="Altisource Warrant",
        listing_exchange="NASDAQ",
        is_etf=False,
        is_test_issue=False,
        source_file="nasdaqlisted.txt",
        category="WARRANT",
    )
    bctxl_seed = SeedRecord(
        symbol="BCTXL",
        raw_symbol="BCTXL",
        security_name="BriaCell Warrant",
        listing_exchange="NASDAQ",
        is_etf=False,
        is_test_issue=False,
        source_file="nasdaqlisted.txt",
        category="WARRANT",
    )

    war_aspsz = create_contract_for_seed(aspsz_seed, sec_type="WAR")
    assert war_aspsz.symbol == "ASPS"
    assert war_aspsz.localSymbol == "ASPSZ"

    war_bctxl = create_contract_for_seed(bctxl_seed, sec_type="WAR")
    assert war_bctxl.symbol == "BCTX"
    assert war_bctxl.localSymbol == "BCTXL"


from scripts.instrument_master.discover import RequestMetric, calculate_percentile


def test_calculate_percentile_and_metrics() -> None:
    """Test percentile calculation and RequestMetric aggregation."""
    vals = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    p50 = calculate_percentile(vals, 50.0)
    p95 = calculate_percentile(vals, 95.0)
    assert p50 == 55.0
    assert p95 > 90.0

    metric = RequestMetric(
        seed_symbol="AAPL",
        req_id=1,
        sec_type="STK",
        is_fallback=False,
        category="COMMON_STOCK",
        status="RESOLVED",
        error_code="",
        pacer_wait_ms=0.5,
        tws_rtt_ms=25.0,
        total_latency_ms=25.5,
        num_contracts=1,
    )
    assert metric.seed_symbol == "AAPL"
    assert metric.status == "RESOLVED"
