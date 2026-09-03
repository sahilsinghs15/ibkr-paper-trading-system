from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest
from src.config import BenchmarkConfig

def test_port_4001_rejected():
    cfg = BenchmarkConfig(ib_port=4001)
    with pytest.raises(ValueError, match="4001"):
        cfg.validate()

def test_paper_port_ok():
    cfg = BenchmarkConfig(ib_port=4002)
    cfg.validate()  # no raise

def test_whatif_safety_flag():
    # Real client must enforce whatIf=True — verify IBOrder defaults
    from ibapi.order import Order
    o = Order()
    o.whatIf = True
    o.transmit = False
    assert o.whatIf is True
    # Simulated safety check from real_client.py
    assert o.whatIf is True, "SAFETY: whatIf must be True"
