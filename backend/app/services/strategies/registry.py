"""In-process strategy registry. Registration is explicit, not dynamic imports."""

from collections.abc import Sequence

from app.services.strategies.handler import StrategyHandler


class StrategyRegistry:
    """First matching handler wins. Unknown strategies fall through to legacy."""

    def __init__(self, handlers: Sequence[StrategyHandler] | None = None) -> None:
        self._handlers: list[StrategyHandler] = list(handlers or [])

    def register(self, handler: StrategyHandler) -> None:
        self._handlers.append(handler)

    def get(self, strategy_id: str | None) -> StrategyHandler | None:
        for handler in self._handlers:
            if handler.can_handle(strategy_id):
                return handler
        return None
