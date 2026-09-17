"""Channel adapters and dispatching abstraction."""

from app.services.notification.channels.base import BaseChannelAdapter
from app.services.notification.channels.sms import SMSChannelAdapter
from app.services.notification.channels.telegram import TelegramChannelAdapter
from app.services.notification.channels.whatsapp import WhatsAppChannelAdapter

__all__ = [
    "BaseChannelAdapter",
    "SMSChannelAdapter",
    "TelegramChannelAdapter",
    "WhatsAppChannelAdapter",
]
