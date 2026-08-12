"""Offline unit tests for NASDAQ Trader seed fetcher using local text fixtures."""

import tempfile
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.instrument_master.seed_fetcher import (
    classify_seed_security,
    fetch_url_content,
    filter_seed_records,
    load_seed_universe_from_files,
    map_exchange_code,
    parse_nasdaqlisted_content,
    parse_otherlisted_content,
)


def test_classify_seed_security() -> None:
    """Test security category classification rules."""
    assert (
        classify_seed_security("Apple Inc. - Common Stock", "AAPL", is_etf=False)
        == "COMMON_STOCK"
    )
    assert (
        classify_seed_security("Invesco QQQ Trust Series 1", "QQQ", is_etf=True)
        == "ETF"
    )
    assert (
        classify_seed_security(
            "ATA Creativity Global - American Depositary Shares", "AACG", is_etf=False
        )
        == "ADR"
    )
    assert (
        classify_seed_security(
            "Armada Acquisition Corp. III - Warrant", "AACIW", is_etf=False
        )
        == "WARRANT"
    )
    assert (
        classify_seed_security(
            "Artius II Acquisition Inc. - Rights", "AACBR", is_etf=False
        )
        == "RIGHT"
    )
    assert (
        classify_seed_security(
            "Armada Acquisition Corp. III - Units", "AACIU", is_etf=False
        )
        == "UNIT"
    )
    assert (
        classify_seed_security("Acme Corp Warrant", "ACME.WS", is_etf=False)
        == "WARRANT"
    )


SAMPLE_NASDAQ_LISTED = """Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N
MSFT|Microsoft Corporation - Common Stock|Q|N|N|100|N|N
QQQ|Invesco QQQ Trust Series 1|Q|N|N|100|Y|N
ZTEST|NASDAQ TEST SECURITY|Q|Y|N|100|N|N
File Creation Time: 0812202612:00|||||||
"""

SAMPLE_OTHER_LISTED = """ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
IBM|International Business Machines Corporation|N|IBM|N|100|N|IBM
SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY
BRK B|Berkshire Hathaway Inc. Class B|N|BRK.B|N|100|N|BRK B
NTEST|NYSE TEST SECURITY|N|NTEST|N|100|Y|NTEST
File Creation Time: 0812202612:00|||||||
"""


def test_parse_nasdaqlisted_content() -> None:
    """Test parsing nasdaqlisted.txt text fixture."""
    records = parse_nasdaqlisted_content(SAMPLE_NASDAQ_LISTED)
    assert len(records) == 4  # AAPL, MSFT, QQQ, ZTEST (header and footer excluded)

    # AAPL check
    aapl = next(r for r in records if r.symbol == "AAPL")
    assert aapl.raw_symbol == "AAPL"
    assert aapl.security_name == "Apple Inc. - Common Stock"
    assert aapl.listing_exchange == "NASDAQ"
    assert not aapl.is_etf
    assert not aapl.is_test_issue
    assert aapl.source_file == "nasdaqlisted.txt"

    # QQQ ETF check
    qqq = next(r for r in records if r.symbol == "QQQ")
    assert qqq.is_etf

    # ZTEST test issue check
    ztest = next(r for r in records if r.symbol == "ZTEST")
    assert ztest.is_test_issue


def test_parse_otherlisted_content() -> None:
    """Test parsing otherlisted.txt text fixture."""
    records = parse_otherlisted_content(SAMPLE_OTHER_LISTED)
    assert len(records) == 4  # IBM, SPY, BRK B, NTEST

    # IBM NYSE equity check
    ibm = next(r for r in records if r.symbol == "IBM")
    assert ibm.raw_symbol == "IBM"
    assert ibm.listing_exchange == "NYSE"
    assert not ibm.is_etf
    assert not ibm.is_test_issue

    # SPY NYSE Arca ETF check
    spy = next(r for r in records if r.symbol == "SPY")
    assert spy.listing_exchange == "NYSE_ARCA"
    assert spy.is_etf

    # NTEST test issue check
    ntest = next(r for r in records if r.symbol == "NTEST")
    assert ntest.is_test_issue


def test_filter_seed_records() -> None:
    """Test filtering test issues."""
    nasdaq_records = parse_nasdaqlisted_content(SAMPLE_NASDAQ_LISTED)
    filtered = filter_seed_records(nasdaq_records, exclude_test_issues=True)

    symbols = [r.symbol for r in filtered]
    assert "AAPL" in symbols
    assert "MSFT" in symbols
    assert "QQQ" in symbols
    assert "ZTEST" not in symbols  # Excluded by filter


def test_map_exchange_code() -> None:
    """Test mapping raw exchange codes."""
    assert map_exchange_code("N", "otherlisted.txt") == "NYSE"
    assert map_exchange_code("P", "otherlisted.txt") == "NYSE_ARCA"
    assert map_exchange_code("A", "otherlisted.txt") == "AMEX"
    assert map_exchange_code("Z", "otherlisted.txt") == "BATS"
    assert map_exchange_code("Q", "nasdaqlisted.txt") == "NASDAQ"
    assert map_exchange_code("X", "otherlisted.txt") == "OTHER_X"


def test_load_seed_universe_from_files() -> None:
    """Test loading seed records from offline local text file fixtures."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        nasdaq_path = Path(tmp_dir) / "nasdaqlisted.txt"
        other_path = Path(tmp_dir) / "otherlisted.txt"

        nasdaq_path.write_text(SAMPLE_NASDAQ_LISTED, encoding="utf-8")
        other_path.write_text(SAMPLE_OTHER_LISTED, encoding="utf-8")

        records = load_seed_universe_from_files(
            nasdaq_path, other_path, exclude_test_issues=True
        )

        # 3 valid from nasdaq (AAPL, MSFT, QQQ) + 3 valid from other (IBM, SPY, BRK B)
        assert len(records) == 6
        symbols = [r.symbol for r in records]
        assert "AAPL" in symbols
        assert "SPY" in symbols
        assert "ZTEST" not in symbols
        assert "NTEST" not in symbols


@patch("urllib.request.urlopen")
def test_fetch_url_content_error_handling(mock_urlopen: MagicMock) -> None:
    """Test network failure raises RuntimeError cleanly."""
    mock_urlopen.side_effect = urllib.error.URLError("Network unreachable")

    with pytest.raises(RuntimeError) as exc_info:
        fetch_url_content("http://example.com/test.txt")

    assert "Network error downloading" in str(exc_info.value)
