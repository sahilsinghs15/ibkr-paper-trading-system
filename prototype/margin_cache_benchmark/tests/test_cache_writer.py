from pathlib import Path
import sys, csv, tempfile
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from src.models import MarginResult, utc_now_iso
from src.cache_writer import write_cache_csv

def test_write_cache():
    results = [
        MarginResult(instrument_type="ETF", symbol="SPY", exchange="ARCA", currency="USD", con_id=123, initial_margin="1000.00", maintenance_margin="800.00", timestamp_utc=utc_now_iso(), status="ok"),
        MarginResult(instrument_type="CFD", symbol="AAPL", exchange="SMART", currency="USD", con_id=None, timestamp_utc=utc_now_iso(), status="failed", error="timeout"),
    ]
    out = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
    out.close()
    write_cache_csv(results, out.name)
    rows = list(csv.DictReader(open(out.name)))
    assert rows[0]["symbol"]=="SPY"
    assert rows[0]["status"]=="ok"
    assert rows[1]["status"]=="failed"
    assert "timestamp_utc" in rows[0]
    assert rows[0]["initial_margin"]=="1000.00"
