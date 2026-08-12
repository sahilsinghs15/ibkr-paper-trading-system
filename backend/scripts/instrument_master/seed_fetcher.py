"""NASDAQ Trader daily symbol directory seed fetcher and parser."""

import argparse
import logging
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

NASDAQ_LISTED_URL = "http://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "http://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

logger = logging.getLogger("seed_fetcher")


@dataclass
class SeedRecord:
    """Structured seed record representation for a US equity or ETF."""

    symbol: str
    raw_symbol: str
    security_name: str
    listing_exchange: str
    is_etf: bool
    is_test_issue: bool
    source_file: str
    category: str = "COMMON_STOCK"

    def to_dict(self) -> dict[str, Any]:
        """Convert record to dictionary format."""
        return asdict(self)


def normalize_symbol(raw_symbol: str) -> str:
    """Normalize raw NASDAQ Trader symbol into standard US equity ticker format."""
    symbol = raw_symbol.strip()
    return symbol


def classify_seed_security(security_name: str, symbol: str, is_etf: bool) -> str:
    """Classify security into category using NASDAQ Trader metadata and ticker syntax."""
    if is_etf:
        return "ETF"

    sec_name_upper = security_name.upper()
    sym_upper = symbol.strip().upper()

    if (
        "WARRANT" in sec_name_upper
        or sym_upper.endswith((".WS", "-WS"))
        or len(sym_upper) >= 5
        and sym_upper.endswith("W")
    ):
        return "WARRANT"

    if (
        "RIGHT" in sec_name_upper
        or sym_upper.endswith((".RT", "-RT"))
        or len(sym_upper) >= 5
        and sym_upper.endswith("R")
    ):
        return "RIGHT"

    if (
        "UNIT" in sec_name_upper
        or sym_upper.endswith((".U", "-U"))
        or len(sym_upper) >= 5
        and sym_upper.endswith("U")
    ):
        return "UNIT"

    if (
        "AMERICAN DEPOSITARY" in sec_name_upper
        or "ADR" in sec_name_upper
        or "ADS" in sec_name_upper
    ):
        return "ADR"

    return "COMMON_STOCK"


def map_exchange_code(code: str, source_file: str) -> str:
    """Map raw exchange code from NASDAQ Trader directory to standard name."""
    code = code.strip().upper()
    if source_file == "nasdaqlisted.txt":
        return "NASDAQ"

    # Mapping for otherlisted.txt exchange codes
    mapping = {
        "A": "AMEX",
        "N": "NYSE",
        "P": "NYSE_ARCA",
        "Z": "BATS",
        "V": "IEX",
        "Q": "NASDAQ",
    }
    return mapping.get(code, f"OTHER_{code}" if code else "OTHER")


def parse_nasdaqlisted_content(
    content: str, source_filename: str = "nasdaqlisted.txt"
) -> list[SeedRecord]:
    """Parse raw pipe-delimited lines from nasdaqlisted.txt."""
    records: list[SeedRecord] = []
    lines = content.strip().splitlines()

    for line_num, line in enumerate(lines, start=1):
        line = line.strip()
        if not line or line.startswith("File Creation Time:"):
            continue

        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 8:
            logger.debug(
                "Skipping line %d in %s (insufficient columns: %d): %s",
                line_num,
                source_filename,
                len(parts),
                line,
            )
            continue

        # Header row check
        if parts[0] == "Symbol" and parts[1] == "Security Name":
            continue

        raw_symbol = parts[0]
        security_name = parts[1]
        test_issue_str = parts[3].upper()
        etf_str = parts[6].upper()

        is_test_issue = test_issue_str == "Y"
        is_etf = etf_str == "Y"
        clean_symbol = normalize_symbol(raw_symbol)
        category = classify_seed_security(security_name, clean_symbol, is_etf)

        records.append(
            SeedRecord(
                symbol=clean_symbol,
                raw_symbol=raw_symbol,
                security_name=security_name,
                listing_exchange="NASDAQ",
                is_etf=is_etf,
                is_test_issue=is_test_issue,
                source_file=source_filename,
                category=category,
            )
        )

    return records


def parse_otherlisted_content(
    content: str, source_filename: str = "otherlisted.txt"
) -> list[SeedRecord]:
    """Parse raw pipe-delimited lines from otherlisted.txt."""
    records: list[SeedRecord] = []
    lines = content.strip().splitlines()

    for line_num, line in enumerate(lines, start=1):
        line = line.strip()
        if not line or line.startswith("File Creation Time:"):
            continue

        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 8:
            logger.debug(
                "Skipping line %d in %s (insufficient columns: %d): %s",
                line_num,
                source_filename,
                len(parts),
                line,
            )
            continue

        # Header row check
        if parts[0] in ("ACT Symbol", "ACTSymbol") and parts[1] == "Security Name":
            continue

        raw_symbol = parts[0]
        security_name = parts[1]
        raw_exchange = parts[2]
        etf_str = parts[4].upper()
        test_issue_str = parts[6].upper()

        is_test_issue = test_issue_str == "Y"
        is_etf = etf_str == "Y"
        clean_symbol = normalize_symbol(raw_symbol)
        listing_exchange = map_exchange_code(raw_exchange, source_filename)
        category = classify_seed_security(security_name, clean_symbol, is_etf)

        records.append(
            SeedRecord(
                symbol=clean_symbol,
                raw_symbol=raw_symbol,
                security_name=security_name,
                listing_exchange=listing_exchange,
                is_etf=is_etf,
                is_test_issue=is_test_issue,
                source_file=source_filename,
                category=category,
            )
        )

    return records


def filter_seed_records(
    records: list[SeedRecord], exclude_test_issues: bool = True
) -> list[SeedRecord]:
    """Apply justified filters (e.g. exclude test issues) to seed records."""
    filtered: list[SeedRecord] = []
    for rec in records:
        if exclude_test_issues and rec.is_test_issue:
            logger.debug("Filtering test issue: %s", rec.raw_symbol)
            continue
        filtered.append(rec)
    return filtered


def fetch_url_content(url: str, timeout: float = 15.0) -> str:
    """Download plain text content from URL or read from local file path."""
    path = Path(url)
    if path.exists():
        logger.info("Loading seed content from local file path: %s", path)
        return path.read_text(encoding="utf-8", errors="replace")

    logger.info("Fetching directory content from %s...", url)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": DEFAULT_USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = response.read().decode("utf-8", errors="replace")
            if data.lstrip().startswith(("<html", "<HTML", "<!DOCTYPE", "<!doctype")):
                raise RuntimeError(
                    f"URL {url} returned HTML block instead of pipe-delimited text (Incapsula WAF block)."
                )
            return data
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        OSError,
        RuntimeError,
    ) as e:
        logger.warning("Failed to fetch content from primary URL %s: %s", url, e)
        raise RuntimeError(f"Network error downloading {url}: {e}") from e


def fetch_nasdaq_seed_universe_from_https_mirror(
    timeout: float = 15.0,
) -> list[SeedRecord]:
    """Fallback fetcher downloading full US equity/ETF ticker universe via HTTPS mirror."""
    import json

    logger.info("Attempting HTTPS fallback fetch from US Stock Symbols repository...")
    base_url = "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main/"
    mirror_files = [
        ("nasdaq/nasdaq_full_tickers.json", "NASDAQ", "nasdaqlisted.txt"),
        ("nyse/nyse_full_tickers.json", "NYSE", "otherlisted.txt"),
        ("amex/amex_full_tickers.json", "AMEX", "otherlisted.txt"),
    ]
    records: list[SeedRecord] = []
    for f_path, exchange, src in mirror_files:
        url = base_url + f_path
        req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                items = json.loads(resp.read().decode("utf-8", errors="replace"))
                for item in items:
                    sym = str(item.get("symbol", "")).strip()
                    name = str(item.get("name", "")).strip()
                    if not sym:
                        continue
                    is_etf = "ETF" in name.upper() or "FUND" in name.upper()
                    cat = classify_seed_security(name, sym, is_etf=is_etf)
                    records.append(
                        SeedRecord(
                            symbol=sym,
                            raw_symbol=sym,
                            security_name=name,
                            listing_exchange=exchange,
                            is_etf=is_etf,
                            is_test_issue=False,
                            source_file=src,
                            category=cat,
                        )
                    )
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to fetch HTTPS mirror %s: %s", url, e)

    return records


def fetch_nasdaq_seed_universe(
    nasdaq_url: str = NASDAQ_LISTED_URL,
    other_url: str = OTHER_LISTED_URL,
    exclude_test_issues: bool = True,
    timeout: float = 15.0,
) -> list[SeedRecord]:
    """Fetch and parse daily seed records from NASDAQ Trader directory files with multi-tier fallback."""
    all_records: list[SeedRecord] = []

    # 1. Process nasdaqlisted.txt
    try:
        nasdaq_raw = fetch_url_content(nasdaq_url, timeout=timeout)
        nasdaq_records = parse_nasdaqlisted_content(nasdaq_raw, "nasdaqlisted.txt")
        logger.info("Parsed %d records from nasdaqlisted.txt", len(nasdaq_records))
        all_records.extend(nasdaq_records)
    except Exception as e:  # noqa: BLE001
        logger.warning("Error processing NASDAQ listed directory: %s", e)

    # 2. Process otherlisted.txt
    try:
        other_raw = fetch_url_content(other_url, timeout=timeout)
        other_records = parse_otherlisted_content(other_raw, "otherlisted.txt")
        logger.info("Parsed %d records from otherlisted.txt", len(other_records))
        all_records.extend(other_records)
    except Exception as e:  # noqa: BLE001
        logger.warning("Error processing other listed directory: %s", e)

    # 3. Fallback to HTTPS ticker mirror if primary HTTP/FTP fetch returned empty due to WAF/firewall
    if not all_records:
        logger.warning(
            "Primary NASDAQ Trader URLs unavailable (WAF/firewall block). Falling back to HTTPS US-Stock-Symbols mirror..."
        )
        all_records = fetch_nasdaq_seed_universe_from_https_mirror(timeout=timeout)

    # 4. Fallback to local CSV cache if HTTPS mirror also fails
    if not all_records:
        local_csvs = list(
            Path(__file__)
            .resolve()
            .parent.parent.parent.glob("data/instrument_master/*.csv")
        )
        if local_csvs:
            latest_csv = max(local_csvs, key=lambda p: p.stat().st_mtime)
            logger.info(
                "Loading seed universe fallback from local CSV cache %s...", latest_csv
            )
            import csv

            with latest_csv.open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    sym = row.get("seed_symbol", "").strip()
                    if sym:
                        all_records.append(
                            SeedRecord(
                                symbol=sym,
                                raw_symbol=row.get("seed_raw_symbol", sym),
                                security_name=row.get("seed_security_name", ""),
                                listing_exchange=row.get("seed_exchange", "SMART"),
                                is_etf=row.get("seed_is_etf", "").lower() == "true",
                                is_test_issue=False,
                                source_file=row.get("seed_source_file", "manual"),
                                category=row.get("seed_category", "COMMON_STOCK"),
                            )
                        )

    if not all_records:
        raise RuntimeError(
            "Failed to fetch or parse any seed records from NASDAQ Trader or fallback mirrors."
        )

    filtered_records = filter_seed_records(
        all_records, exclude_test_issues=exclude_test_issues
    )
    logger.info(
        "Total seed records fetched: %d (after filtering test issues: %d)",
        len(all_records),
        len(filtered_records),
    )
    return filtered_records


def load_seed_universe_from_files(
    nasdaq_file_path: Path,
    other_file_path: Path,
    exclude_test_issues: bool = True,
) -> list[SeedRecord]:
    """Offline helper to load seed records from local text file fixtures."""
    all_records: list[SeedRecord] = []

    if nasdaq_file_path.exists():
        content = nasdaq_file_path.read_text(encoding="utf-8")
        all_records.extend(parse_nasdaqlisted_content(content, nasdaq_file_path.name))

    if other_file_path.exists():
        content = other_file_path.read_text(encoding="utf-8")
        all_records.extend(parse_otherlisted_content(content, other_file_path.name))

    return filter_seed_records(all_records, exclude_test_issues=exclude_test_issues)


def setup_logging(verbose: bool = False) -> None:
    """Configure console logging."""
    log_level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    logger.setLevel(log_level)
    logger.addHandler(handler)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="NASDAQ Trader Daily Symbol Directory Seed Fetcher."
    )
    parser.add_argument(
        "--nasdaq-url",
        default=NASDAQ_LISTED_URL,
        help="URL or local path to nasdaqlisted.txt",
    )
    parser.add_argument(
        "--other-url",
        default=OTHER_LISTED_URL,
        help="URL or local path to otherlisted.txt",
    )
    parser.add_argument(
        "--include-test-issues",
        action="store_true",
        help="Include test issues in output (default: excluded)",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print summary stats of fetched seed universe",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose log output"
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    logger.info("Fetching NASDAQ Trader daily symbol directory seed universe...")
    try:
        seeds = fetch_nasdaq_seed_universe(
            nasdaq_url=args.nasdaq_url,
            other_url=args.other_url,
            exclude_test_issues=not args.include_test_issues,
        )

        etf_count = sum(1 for s in seeds if s.is_etf)
        equity_count = len(seeds) - etf_count

        logger.info(
            "SUCCESS: Fetched %d seed records (%d equities, %d ETFs).",
            len(seeds),
            equity_count,
            etf_count,
        )

        if args.summary:
            print("\n--- SEED UNIVERSE SUMMARY ---")
            print(f"Total Symbols  : {len(seeds)}")
            print(f"Equities       : {equity_count}")
            print(f"ETFs           : {etf_count}")
            exchanges: dict[str, int] = {}
            for s in seeds:
                exchanges[s.listing_exchange] = exchanges.get(s.listing_exchange, 0) + 1
            print("By Exchange    :")
            for exch, count in sorted(exchanges.items()):
                print(f"  - {exch:12s}: {count}")
            print("Sample Symbols :", [s.symbol for s in seeds[:10]])

        sys.exit(0)
    except Exception as e:  # noqa: BLE001
        logger.error("FAILURE: Seed fetcher failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
