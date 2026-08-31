"""Lightweight watchdog — observe, notify, verify, escalate.

Not a second process manager.
"""

from app.services.watchdog.config import WatchdogSettings, get_watchdog_settings
from app.services.watchdog.daemon import WatchdogDaemon
from app.services.watchdog.models import NotificationEvent, ServiceName, ServiceState

__all__ = ["NotificationEvent", "ServiceName", "ServiceState", "WatchdogDaemon", "WatchdogSettings", "get_watchdog_settings"]
