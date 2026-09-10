"""Current-day IBKR execution source.

IB Gateway/TWS is the sole source: same-day executions only.
Trade Book accumulates across days in PostgreSQL.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.broker.ibkr.executions import BrokerExecutionLine

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecutionFetchResult:
    lines: list[BrokerExecutionLine]
    timed_out: bool
    source: str  # "current"


class ExecutionSource(ABC):
    """Pluggable execution source."""

    @abstractmethod
    async def fetch(self, *, ibkr_account: str) -> ExecutionFetchResult:
        """Fetch executions for account."""


class CurrentGatewayExecutionSource(ExecutionSource):
    """Adapter wrapping TWSClient.request_executions_async as ExecutionSource."""

    def __init__(self, client) -> None:
        self._client = client

    async def fetch(self, *, ibkr_account: str) -> ExecutionFetchResult:
        lines, timed_out = await self._client.request_executions_async(ibkr_account=ibkr_account, timeout=15.0)
        clean = ibkr_account.strip().upper()
        filtered = [line for line in lines if line.ibkr_account.strip().upper() == clean]
        return ExecutionFetchResult(lines=filtered, timed_out=bool(timed_out), source="current")
