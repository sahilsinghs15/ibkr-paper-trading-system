"""Channel dispatcher routing deliveries to appropriate channel adapters."""

from __future__ import annotations

import logging

from app.db.models.notification import NotificationDeliveryModel, NotificationLogModel
from app.services.notification.channels.base import BaseChannelAdapter
from app.services.notification.channels.sms import SMSChannelAdapter
from app.services.notification.channels.telegram import TelegramChannelAdapter
from app.services.notification.channels.whatsapp import WhatsAppChannelAdapter
from app.services.notification.types import ChannelType, DeliveryResult

logger = logging.getLogger(__name__)


class ChannelDispatcher:
    """Registry and dispatcher for channel-specific adapters."""

    def __init__(self) -> None:
        self._adapters: dict[str, BaseChannelAdapter] = {}

    def register(self, adapter: BaseChannelAdapter) -> None:
        """Register a channel adapter instance."""
        channel_key = (
            adapter.channel.value
            if isinstance(adapter.channel, ChannelType)
            else str(adapter.channel).upper()
        )
        self._adapters[channel_key] = adapter
        logger.debug("Registered channel adapter for %s: %s", channel_key, adapter.__class__.__name__)

    def get_adapter(self, channel: str | ChannelType) -> BaseChannelAdapter | None:
        """Retrieve adapter by channel name."""
        channel_key = channel.value if isinstance(channel, ChannelType) else channel.upper()
        return self._adapters.get(channel_key)

    async def dispatch(
        self,
        delivery: NotificationDeliveryModel,
        logical: NotificationLogModel,
    ) -> DeliveryResult:
        """Route delivery to registered adapter."""
        adapter = self.get_adapter(delivery.channel)
        if adapter is None:
            logger.error(
                "No adapter registered for channel '%s' (delivery_id=%s)",
                delivery.channel,
                delivery.delivery_id,
            )
            return DeliveryResult(
                success=False,
                error_message=f"No adapter registered for channel '{delivery.channel}'",
                is_retryable=False,
                error_details={"reason": "UNSUPPORTED_CHANNEL", "channel": delivery.channel},
            )

        return await adapter.send(delivery, logical)


_default_dispatcher: ChannelDispatcher | None = None


def get_default_dispatcher() -> ChannelDispatcher:
    """Return singleton channel dispatcher preconfigured with all configured active adapters.

    Each adapter is registered independently. A misconfigured or disabled channel
    does not prevent other channels from being registered or dispatched.
    """
    global _default_dispatcher
    if _default_dispatcher is None:
        from app.core.config import get_settings

        settings = get_settings()
        dispatcher = ChannelDispatcher()

        # Telegram — always registered (adapter handles disabled/unconfigured state internally)
        dispatcher.register(TelegramChannelAdapter())

        # SMS — registered when enabled; dispatches are safely skipped if not configured
        if settings.sms_enabled:
            try:
                dispatcher.register(SMSChannelAdapter())
                logger.info("SMS channel adapter registered (region=%s)", settings.sms_aws_region)
            except Exception:
                logger.exception(
                    "Failed to register SMS channel adapter; SMS channel will be unavailable"
                )

        # WhatsApp — registered when enabled; dispatches are safely skipped if not configured
        if settings.whatsapp_enabled:
            try:
                dispatcher.register(WhatsAppChannelAdapter())
                logger.info("WhatsApp channel adapter registered (api_version=%s)", settings.whatsapp_api_version)
            except Exception:
                logger.exception(
                    "Failed to register WhatsApp channel adapter; WhatsApp channel will be unavailable"
                )

        _default_dispatcher = dispatcher
    return _default_dispatcher


def reset_default_dispatcher() -> None:
    """Reset the singleton dispatcher (intended for use in tests only)."""
    global _default_dispatcher
    _default_dispatcher = None
