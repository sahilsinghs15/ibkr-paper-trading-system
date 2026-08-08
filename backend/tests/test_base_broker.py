"""Tests for base broker abstraction."""

import inspect

import pytest

from app.broker.base_broker import BaseBroker


class TestBaseBroker:
    def test_cannot_instantiate_directly(self) -> None:
        """BaseBroker is abstract and must not be instantiated."""
        with pytest.raises(TypeError):
            BaseBroker()  # type: ignore[abstract]

    def test_exposes_all_required_abstract_operations(self) -> None:
        """BaseBroker must define the complete set of broker operations."""
        expected = {
            "login",
            "disconnect",
            "get_order_book",
            "get_positions",
            "get_margin",
            "place_order",
            "modify_order",
            "cancel_order",
        }
        actual = frozenset(BaseBroker.__abstractmethods__)
        assert actual == expected

    def test_all_operations_are_async(self) -> None:
        """Every abstract method should be a coroutine function."""
        for name in BaseBroker.__abstractmethods__:
            method = getattr(BaseBroker, name)
            assert inspect.iscoroutinefunction(method), f"{name} should be async"
