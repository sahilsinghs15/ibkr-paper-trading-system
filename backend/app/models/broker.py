"""Broker-related domain models."""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class BrokerStatus(Enum):
    """Connection status of a broker."""

    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    ERROR = "ERROR"


@dataclass(frozen=True)
class Margin:
    """Account margin information.

    A clean domain model rather than a raw dictionary, providing
    the essential margin fields for a paper trading system.
    """

    equity: Decimal
    available_funds: Decimal
    buying_power: Decimal
