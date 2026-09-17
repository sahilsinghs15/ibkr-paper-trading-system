"""Base interface for all notification channel adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.db.models.notification import NotificationDeliveryModel, NotificationLogModel
from app.services.notification.types import ChannelType, DeliveryResult


class BaseChannelAdapter(ABC):
    """Abstract base adapter for channel-specific external delivery providers."""

    @property
    @abstractmethod
    def channel(self) -> ChannelType:
        """The channel type handled by this adapter."""
        ...

    @abstractmethod
    async def send(
        self,
        delivery: NotificationDeliveryModel,
        logical: NotificationLogModel,
    ) -> DeliveryResult:
        """Deliver the logical notification through the provider.

        Must NEVER raise an unhandled exception to the caller.
        Must classify errors into retryable vs non-retryable in DeliveryResult.
        """
        ...
