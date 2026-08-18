"""Test defaults: keep the paper STK→CFD override off unless a test enables it."""

import os

os.environ.setdefault("PAPER_EXECUTE_STK_AS_CFD", "false")
