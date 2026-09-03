import csv
import tempfile
from pathlib import Path
import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from src.csv_loader import load_instruments, write_instruments
from src.models import Instrument


def _write_tmp(rows):
    tmp = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".csv", newline="")
    w = csv.DictWriter(tmp, fieldnames=["instrument_type","symbol","exchange","currency"])
    w.writeheader()
    for r in rows:
        w.writerow(r)
    tmp.close()
    return tmp.name

def test_load_valid():
    path = _write_tmp([
        {"instrument_type":"ETF","symbol":"SPY","exchange":"ARCA","currency":"USD"},
        {"instrument_type":"CFD","symbol":"AAPL","exchange":"SMART","currency":"USD"},
    ])
    insts = load_instruments(path)
    assert len(insts)==2

def test_duplicate_detection():
    path = _write_tmp([
        {"instrument_type":"ETF","symbol":"SPY","exchange":"ARCA","currency":"USD"},
        {"instrument_type":"ETF","symbol":"SPY","exchange":"ARCA","currency":"USD"},
    ])
    with pytest.raises(ValueError, match="Duplicate"):
        load_instruments(path)

def test_invalid_etf_exchange():
    path = _write_tmp([{"instrument_type":"ETF","symbol":"SPY","exchange":"SMART","currency":"USD"}])
    with pytest.raises(ValueError, match="ETF exchange"):
        load_instruments(path)

def test_nyse_arca_normalized():
    path = _write_tmp([{"instrument_type":"ETF","symbol":"SPY","exchange":"NYSE ARCA","currency":"USD"}])
    insts = load_instruments(path)
    assert insts[0].exchange=="ARCA"

def test_instrument_count():
    instruments = load_instruments(Path(__file__).resolve().parent.parent / "data" / "instruments.csv")
    assert len(instruments)==1000
    assert sum(1 for i in instruments if i.instrument_type=="ETF")==500
    assert sum(1 for i in instruments if i.instrument_type=="CFD")==500
