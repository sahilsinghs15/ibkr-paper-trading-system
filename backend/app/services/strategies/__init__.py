"""Lightweight strategy dispatch for the modular monolith.

Adding a strategy: implement StrategyHandler, register it, add tests.
Do not rewrite RMS, OMS, or the IBKR adapter.
"""

from app.services.strategies.handler import StrategyHandler
from app.services.strategies.registry import StrategyRegistry

__all__ = [
    "StrategyHandler",
    "StrategyRegistry",
]
