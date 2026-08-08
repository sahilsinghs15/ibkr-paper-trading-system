"""Tests for base strategy abstraction."""

import pytest

from app.strategy.base_strategy import BaseStrategy


class TestBaseStrategy:
    def test_cannot_instantiate_directly(self) -> None:
        """BaseStrategy is abstract and must not be instantiated."""
        with pytest.raises(TypeError):
            BaseStrategy()  # type: ignore[abstract]
