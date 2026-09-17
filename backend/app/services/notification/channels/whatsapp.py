"""WhatsApp channel adapter using the Meta WhatsApp Cloud API (Graph API).

Authentication uses a Bearer access token (system user token for production).
The token must be set via WHATSAPP_ACCESS_TOKEN environment variable or Settings.
It is NEVER logged in error output.

Message delivery uses an approved utility template. WhatsApp Cloud API requires
pre-approved templates for all proactive (business-initiated) messages outside
the 24-hour customer service window. The default template is 'oems_alert' with
two body variables:
    {{1}} = notification title   (≤ 60 chars; truncated if needed)
    {{2}} = notification message (≤ 200 chars; truncated if needed)

The template must be approved in WhatsApp Manager before use.

Error classification by HTTP status and Meta error code:
  Retryable:
    - HTTP 429, 500, 502, 503, 504
    - Meta error 130429 (rate limit / throughput exceeded)
    - Network / timeout errors
  Permanent:
    - HTTP 400, 401, 403, 404
    - Meta error 131031 (business account locked)
    - Meta error 131047 (template not found / unapproved)
    - Meta error 130472 (user in experiment — marketing holdout)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from app.core.config import get_settings
from app.db.models.notification import NotificationDeliveryModel, NotificationLogModel
from app.services.notification.channels.base import BaseChannelAdapter
from app.services.notification.types import ChannelType, DeliveryResult

logger = logging.getLogger(__name__)

# Template variable character limits
_TITLE_MAX = 60
_MESSAGE_MAX = 200

# Meta error codes that are retryable (transient)
_RETRYABLE_META_CODES: frozenset[int] = frozenset(
    {
        130429,  # Rate limit / throughput exceeded — retry with backoff
        4,       # Application request limit reached
        80007,   # Rate limit from account
    }
)

# Meta error codes that are permanent (non-retryable)
_PERMANENT_META_CODES: frozenset[int] = frozenset(
    {
        131031,  # Business Account locked
        131047,  # Template not found or unapproved
        130472,  # User in experiment (marketing holdout)
        131026,  # Recipient phone number not in allowed list
        131050,  # User's number blocked
        100,     # Invalid parameter
        10,      # Permission denied
        190,     # Access token expired / invalid
    }
)


def _truncate(text: str, max_len: int) -> str:
    """Truncate text to max_len characters, adding ellipsis if truncated."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _extract_meta_error_code(response_json: dict[str, Any]) -> int | None:
    """Extract Meta error code from WhatsApp Cloud API JSON response."""
    try:
        error = response_json.get("error", {})
        # Meta wraps the actual WA error inside error.error_data
        wa_code = error.get("error_data", {}).get("details")
        if wa_code is not None:
            return int(wa_code)
        return int(error.get("code", 0)) or None
    except (TypeError, ValueError, AttributeError):
        return None


class WhatsAppChannelAdapter(BaseChannelAdapter):
    """Channel adapter translating generic deliveries into Meta WhatsApp Cloud API messages.

    Uses an approved utility template to deliver OEMS notifications as proactive
    WhatsApp messages. All HTTP calls use httpx (already a project dependency).
    The access token is never logged.
    """

    def __init__(
        self,
        *,
        access_token: str | None = None,
        phone_number_id: str | None = None,
        recipient_phone: str | None = None,
        template_name: str | None = None,
        template_language: str | None = None,
        api_version: str | None = None,
        timeout_seconds: float | None = None,
        enabled: bool | None = None,
        rate_limit_per_sec: float = 2.0,
    ) -> None:
        settings = get_settings()

        self._enabled = (
            enabled if enabled is not None else settings.whatsapp_enabled
        )
        self._access_token = access_token or settings.whatsapp_access_token or ""
        self._phone_number_id = (
            phone_number_id or settings.whatsapp_phone_number_id or ""
        )
        self._recipient_phone = (
            recipient_phone or settings.whatsapp_recipient_phone or ""
        )
        self._template_name = template_name or settings.whatsapp_template_name
        self._template_language = (
            template_language or settings.whatsapp_template_language
        )
        self._api_version = api_version or settings.whatsapp_api_version
        self._timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.whatsapp_timeout_seconds
        )
        self._rate_limit_per_sec = max(0.1, rate_limit_per_sec)
        self._last_send_ts: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def channel(self) -> ChannelType:
        return ChannelType.WHATSAPP

    @property
    def is_configured(self) -> bool:
        """True if the adapter has valid credentials and is enabled."""
        return bool(self._enabled and self._access_token and self._phone_number_id)

    def _api_url(self) -> str:
        return (
            f"https://graph.facebook.com/{self._api_version}"
            f"/{self._phone_number_id}/messages"
        )

    def _build_payload(
        self,
        to: str,
        title: str,
        message: str,
    ) -> dict[str, Any]:
        """Build the WhatsApp template message payload.

        Variables:
            {{1}} → title   (OEMS notification title)
            {{2}} → message (OEMS notification body)
        """
        return {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": self._template_name,
                "language": {"code": self._template_language},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": _truncate(title, _TITLE_MAX)},
                            {"type": "text", "text": _truncate(message, _MESSAGE_MAX)},
                        ],
                    }
                ],
            },
        }

    async def _apply_rate_limit(self) -> None:
        """Pace WhatsApp sends according to rate_limit_per_sec."""
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
        """Deliver logical notification via Meta WhatsApp Cloud API template message.

        Never raises unhandled exceptions. Classifies Meta errors as retryable or permanent.
        The access token is never logged.
        """
        if not self._enabled:
            logger.debug("WhatsApp delivery %s skipped: adapter disabled", delivery.delivery_id)
            return DeliveryResult(
                success=False,
                error_message="WhatsApp channel adapter is disabled",
                is_retryable=False,
                error_details={"reason": "CHANNEL_DISABLED"},
            )

        if not self._access_token or not self._phone_number_id:
            return DeliveryResult(
                success=False,
                error_message="WhatsApp access_token or phone_number_id not configured",
                is_retryable=False,
                error_details={"reason": "UNCONFIGURED_CREDENTIALS"},
            )

        # Recipient: delivery record takes priority
        to = delivery.recipient
        if not to or to == "default":
            to = self._recipient_phone
        if not to:
            return DeliveryResult(
                success=False,
                error_message="WhatsApp recipient phone number not configured",
                is_retryable=False,
                error_details={"reason": "UNCONFIGURED_RECIPIENT"},
            )

        await self._apply_rate_limit()

        payload = self._build_payload(to, logical.title, logical.message)
        # Deliberately omit the access token from any log output
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                resp = await client.post(self._api_url(), json=payload, headers=headers)
                status_code = resp.status_code

                if status_code == 200:
                    data = resp.json()
                    # Successful response: {"messages": [{"id": "..."}]}
                    message_id: str | None = None
                    messages = data.get("messages", [])
                    if messages and isinstance(messages, list):
                        message_id = str(messages[0].get("id") or "")
                    logger.info(
                        "WhatsApp delivery succeeded: delivery_id=%s notification_id=%s message_id=%s",
                        delivery.delivery_id,
                        logical.notification_id,
                        message_id,
                    )
                    return DeliveryResult(
                        success=True,
                        provider_message_id=message_id,
                        is_retryable=False,
                    )

                # Parse error details for classification
                err_data: dict[str, Any] = {}
                try:
                    err_data = resp.json()
                except (ValueError, AttributeError):
                    pass

                meta_error_code = _extract_meta_error_code(err_data)
                err_message_str = (
                    err_data.get("error", {}).get("message", "")
                    or resp.text[:200]
                )

                # Rate-limiting and transient server errors: retryable
                if status_code in (429, 500, 502, 503, 504) or meta_error_code in _RETRYABLE_META_CODES:
                    is_retryable = True
                elif meta_error_code in _PERMANENT_META_CODES:
                    is_retryable = False
                elif status_code in (400, 401, 403, 404):
                    # Bad auth, bad params, forbidden, not found — all permanent
                    is_retryable = False
                else:
                    # Unknown error — conservative: mark retryable
                    is_retryable = True

                if is_retryable:
                    logger.warning(
                        "WhatsApp delivery transient error HTTP %d (meta_code=%s) for delivery %s",
                        status_code,
                        meta_error_code,
                        delivery.delivery_id,
                    )
                else:
                    logger.error(
                        "WhatsApp delivery permanent error HTTP %d (meta_code=%s) for delivery %s",
                        status_code,
                        meta_error_code,
                        delivery.delivery_id,
                    )

                return DeliveryResult(
                    success=False,
                    error_message=f"HTTP {status_code}: {err_message_str}",
                    is_retryable=is_retryable,
                    error_details={
                        "http_status": status_code,
                        "meta_error_code": meta_error_code,
                        "reason": "TRANSIENT_PROVIDER_ERROR" if is_retryable else "PERMANENT_PROVIDER_ERROR",
                    },
                )

        except (httpx.TimeoutException, httpx.NetworkError) as net_exc:
            logger.warning(
                "WhatsApp delivery network error for delivery %s: %s",
                delivery.delivery_id,
                net_exc.__class__.__name__,
            )
            return DeliveryResult(
                success=False,
                error_message=f"Network error: {net_exc.__class__.__name__}",
                is_retryable=True,
                error_details={"reason": "NETWORK_ERROR"},
            )
        except Exception as exc:
            logger.exception(
                "Unexpected exception during WhatsApp delivery %s", delivery.delivery_id
            )
            return DeliveryResult(
                success=False,
                error_message=f"Unexpected error: {exc.__class__.__name__}",
                is_retryable=True,
                error_details={"reason": "UNEXPECTED_ERROR"},
            )
