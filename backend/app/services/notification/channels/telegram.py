"""Dedicated Telegram channel adapter implementing the BaseChannelAdapter interface."""

from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from typing import Any

import httpx

from app.core.config import get_settings
from app.db.models.notification import NotificationDeliveryModel, NotificationLogModel
from app.services.notification.channels.base import BaseChannelAdapter
from app.services.notification.types import (
    ChannelType,
    DeliveryResult,
    NotificationSeverity,
)

logger = logging.getLogger(__name__)


def _get_severity_icon(severity: str, event_type: str = "") -> str:
    """Return appropriate icon for notification severity."""
    sev_upper = (severity or "").upper()
    if "RECOVER" in event_type.upper() or "RESOLV" in event_type.upper():
        return "🟢"
    if sev_upper == NotificationSeverity.CRITICAL.value:
        return "🚨"
    if sev_upper == NotificationSeverity.WARNING.value:
        return "⚠️"
    return "ℹ️"


class TelegramChannelAdapter(BaseChannelAdapter):
    """Channel adapter translating generic deliveries into Telegram Bot API sendMessage requests."""

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
        *,
        timeout_seconds: float = 5.0,
        rate_limit_per_sec: float = 1.0,
        enabled: bool | None = None,
    ) -> None:
        settings = get_settings()

        # Token resolution priority: explicit arg -> Settings -> os.environ
        self._bot_token = (
            bot_token
            or settings.telegram_bot_token
            or os.environ.get("TELEGRAM_BOT_TOKEN")
        )
        self._chat_id = (
            chat_id
            or settings.telegram_chat_id
            or os.environ.get("TELEGRAM_CHAT_ID")
        )

        # Enabled flag resolution
        if enabled is not None:
            self._enabled = enabled
        else:
            env_enabled = os.environ.get("TELEGRAM_ENABLED", "").lower() in ("true", "1", "yes")
            self._enabled = settings.telegram_enabled or env_enabled

        self._timeout_seconds = timeout_seconds or settings.telegram_timeout_seconds
        self._rate_limit_per_sec = rate_limit_per_sec or settings.telegram_rate_limit_per_sec
        self._last_send_ts: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def channel(self) -> ChannelType:
        return ChannelType.TELEGRAM

    @property
    def is_configured(self) -> bool:
        """True if the adapter has valid credentials and is enabled."""
        return bool(self._enabled and self._bot_token and self._chat_id)

    def format_message_text(self, logical: NotificationLogModel) -> str:
        """Format a logical notification into a clean Telegram HTML message."""
        icon = _get_severity_icon(logical.severity, logical.category)
        escaped_title = html.escape(logical.title)
        escaped_msg = html.escape(logical.message)

        if escaped_msg and escaped_msg != escaped_title:
            return f"{icon} <b>{escaped_title}</b>\n{escaped_msg}"
        return f"{icon} <b>{escaped_title}</b>"

    async def _apply_rate_limit(self) -> None:
        """Pace requests according to rate_limit_per_sec."""
        if self._rate_limit_per_sec <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            min_interval = 1.0 / self._rate_limit_per_sec
            wait = self._last_send_ts + min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_send_ts = time.monotonic()

    async def send(
        self,
        delivery: NotificationDeliveryModel,
        logical: NotificationLogModel,
    ) -> DeliveryResult:
        """Deliver logical notification to Telegram Bot API.

        Never raises unhandled exceptions. Classifies failure as retryable vs permanent.
        """
        # Recipient is stored on the delivery record or falls back to adapter default
        recipient_chat_id = delivery.recipient or self._chat_id
        if not self._bot_token or not recipient_chat_id:
            return DeliveryResult(
                success=False,
                error_message="Telegram bot_token or recipient chat_id not configured",
                is_retryable=False,
                error_details={"reason": "UNCONFIGURED_CREDENTIALS"},
            )

        if not self._enabled:
            logger.debug("Telegram delivery %s skipped: adapter disabled", delivery.delivery_id)
            return DeliveryResult(
                success=False,
                error_message="Telegram channel adapter is disabled",
                is_retryable=False,
                error_details={"reason": "CHANNEL_DISABLED"},
            )

        await self._apply_rate_limit()

        text = self.format_message_text(logical)
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": recipient_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                resp = await client.post(url, json=payload)
                status_code = resp.status_code

                if status_code == 200:
                    data = resp.json()
                    msg_id = None
                    if data.get("ok"):
                        msg_id = str(data.get("result", {}).get("message_id") or "")
                    logger.info(
                        "Telegram delivery succeeded: delivery_id=%s notification_id=%s msg_id=%s",
                        delivery.delivery_id,
                        logical.notification_id,
                        msg_id,
                    )
                    return DeliveryResult(
                        success=True,
                        provider_message_id=msg_id,
                        is_retryable=False,
                    )

                # Transient / Rate-limit errors (Retryable)
                if status_code in (429, 500, 502, 503, 504):
                    logger.warning(
                        "Telegram delivery transient error HTTP %d for delivery %s",
                        status_code,
                        delivery.delivery_id,
                    )
                    error_info: dict[str, Any] = {"http_status": status_code}
                    try:
                        err_json = resp.json()
                        error_info["telegram_error"] = err_json.get("description")
                        retry_after = err_json.get("parameters", {}).get("retry_after")
                        if retry_after is not None:
                            error_info["retry_after"] = retry_after
                    except (ValueError, KeyError, AttributeError):
                        logger.debug("Could not parse JSON error payload from Telegram")
                    return DeliveryResult(
                        success=False,
                        error_message=f"HTTP {status_code} transient error from Telegram",
                        is_retryable=True,
                        error_details=error_info,
                    )

                # Permanent configuration / bad request errors (Non-retryable)
                # 400 (Bad request / invalid chat), 401 (Invalid token), 403 (Bot blocked by user), 404 (Not found)
                logger.error(
                    "Telegram delivery permanent error HTTP %d for delivery %s",
                    status_code,
                    delivery.delivery_id,
                )
                try:
                    desc = resp.json().get("description", resp.text[:200])
                except (ValueError, KeyError, AttributeError):
                    desc = resp.text[:200]
                return DeliveryResult(
                    success=False,
                    error_message=f"HTTP {status_code} permanent error: {desc}",
                    is_retryable=False,
                    error_details={"http_status": status_code, "description": desc},
                )

        except (httpx.TimeoutException, httpx.NetworkError) as net_exc:
            logger.warning(
                "Telegram delivery network error for delivery %s: %s",
                delivery.delivery_id,
                net_exc.__class__.__name__,
            )
            return DeliveryResult(
                success=False,
                error_message=f"Network error: {net_exc.__class__.__name__}",
                is_retryable=True,
                error_details={"exception": str(net_exc)},
            )
        except Exception as exc:
            logger.exception(
                "Unexpected exception during Telegram delivery %s", delivery.delivery_id
            )
            return DeliveryResult(
                success=False,
                error_message=f"Unexpected error: {exc.__class__.__name__}",
                is_retryable=True,
                error_details={"exception": str(exc)},
            )
