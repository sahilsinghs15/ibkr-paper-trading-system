"""SMS channel adapter using AWS End User Messaging SMS v2 (pinpoint-sms-voice-v2).

Authentication uses ambient AWS credentials — IAM role, environment variables
(AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN), or
~/.aws/credentials. Never hardcode AWS credentials in source code.

Error classification is provider-aware:
  Retryable:
    - ThrottlingException (SDK automatically retries, but we still mark retryable)
    - InternalErrorException
    - ServiceUnavailableException
    - Network / timeout errors
  Permanent:
    - InvalidParameterException (bad phone number, missing DLT params, etc.)
    - ResourceNotFoundException (invalid OriginationIdentity ARN)
    - AccessDeniedException (credential / permission issue)
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import textwrap
import time
from typing import Any

from app.core.config import get_settings
from app.db.models.notification import NotificationDeliveryModel, NotificationLogModel
from app.services.notification.channels.base import BaseChannelAdapter
from app.services.notification.types import ChannelType, DeliveryResult

logger = logging.getLogger(__name__)

# Dedicated bounded executor for blocking boto3 calls — prevents
# notification SMS delivery from starving the global default executor
# used by trading/OEMS workers. Max 3 ensures at most 3 concurrent
# AWS SMS sends even if worker batch_size is 10.
_SMS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=3,
    thread_name_prefix="sms-channel",
)

# Max SMS message length before truncation (single SMS segment = 160 GSM-7 chars;
# keep well under multi-part boundary; use 155 to leave room for ellipsis).
_SMS_MAX_CHARS = 155

# AWS botocore error codes that are permanent (non-retryable)
_PERMANENT_AWS_ERRORS: frozenset[str] = frozenset(
    {
        "InvalidParameterException",
        "ResourceNotFoundException",
        "AccessDeniedException",
        "ValidationException",
        "ServiceQuotaExceededException",  # Account-level hard quota — not transient throttling
    }
)

# AWS botocore error codes that are retryable (transient)
_RETRYABLE_AWS_ERRORS: frozenset[str] = frozenset(
    {
        "ThrottlingException",
        "TooManyRequestsException",
        "InternalServerException",
        "InternalErrorException",
        "ServiceUnavailableException",
    }
)


def _format_sms_text(logical: NotificationLogModel) -> str:
    """Format a logical notification into a concise plain-text SMS message.

    Truncates gracefully to _SMS_MAX_CHARS, preserving the most critical
    operational information (title first, then message).
    """
    severity_prefix = {
        "CRITICAL": "[CRITICAL] ",
        "WARNING": "[WARN] ",
        "INFO": "[INFO] ",
    }.get(logical.severity.upper(), "")

    # Build full text; truncate if needed
    full_text = f"{severity_prefix}{logical.title}: {logical.message}"
    if len(full_text) <= _SMS_MAX_CHARS:
        return full_text

    # Truncate with ellipsis; always preserve severity prefix + title
    header = f"{severity_prefix}{logical.title}: "
    available = _SMS_MAX_CHARS - len(header) - 3  # 3 for "..."
    if available > 10:
        return header + logical.message[:available] + "..."
    # Title itself too long — truncate the full string
    return textwrap.shorten(full_text, width=_SMS_MAX_CHARS, placeholder="...")


class SMSChannelAdapter(BaseChannelAdapter):
    """Channel adapter translating generic deliveries into AWS End User Messaging SMS requests.

    Uses AWS pinpoint-sms-voice-v2 (the current SMS v2 API, not the legacy Pinpoint v1).
    boto3 calls are synchronous; they are dispatched via run_in_executor to avoid
    blocking the asyncio event loop.
    """

    def __init__(
        self,
        *,
        origination_identity: str | None = None,
        recipient_phone: str | None = None,
        aws_region: str | None = None,
        message_type: str | None = None,
        timeout_seconds: float | None = None,
        india_dlt_principal_entity_id: str | None = None,
        india_dlt_template_id: str | None = None,
        enabled: bool | None = None,
        rate_limit_per_sec: float = 5.0,
    ) -> None:
        settings = get_settings()

        self._enabled = (
            enabled if enabled is not None else settings.sms_enabled
        )
        self._origination_identity = (
            origination_identity or settings.sms_origination_identity or ""
        )
        self._recipient_phone = (
            recipient_phone or settings.sms_recipient_phone or ""
        )
        self._aws_region = aws_region or settings.sms_aws_region or "ap-south-1"
        self._message_type = (
            message_type or settings.sms_message_type or "TRANSACTIONAL"
        )
        self._timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.sms_timeout_seconds
        )
        self._india_dlt_principal_entity_id = (
            india_dlt_principal_entity_id or settings.sms_india_dlt_principal_entity_id
        )
        self._india_dlt_template_id = (
            india_dlt_template_id or settings.sms_india_dlt_template_id
        )
        self._rate_limit_per_sec = max(0.1, rate_limit_per_sec)
        self._last_send_ts: float = 0.0
        self._lock = asyncio.Lock()

        # Lazy boto3 client; created on first send() to avoid import-time errors
        # when boto3 is optional or credentials are absent in test environments.
        self._client: Any | None = None

    @property
    def channel(self) -> ChannelType:
        return ChannelType.SMS

    @property
    def is_configured(self) -> bool:
        """True if the adapter has necessary configuration and is enabled."""
        return bool(self._enabled and self._origination_identity)

    def _get_boto_client(self) -> Any:
        """Return (and lazily create) the boto3 SMS v2 client."""
        if self._client is None:
            import boto3

            self._client = boto3.client(
                "pinpoint-sms-voice-v2",
                region_name=self._aws_region,
            )
        return self._client

    async def _apply_rate_limit(self) -> None:
        """Pace SMS sends according to rate_limit_per_sec."""
        async with self._lock:
            now = time.monotonic()
            min_interval = 1.0 / self._rate_limit_per_sec
            wait = self._last_send_ts + min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_send_ts = time.monotonic()

    def _build_destination_country_params(self) -> dict[str, Any]:
        """Build India DLT destination country parameters if configured."""
        if not (self._india_dlt_principal_entity_id and self._india_dlt_template_id):
            return {}
        return {
            "DestinationCountryParameters": {
                "IN_TEMPLATE_ID": self._india_dlt_template_id,
                "IN_ENTITY_ID": self._india_dlt_principal_entity_id,
            }
        }

    def _send_sms_sync(
        self,
        destination_phone: str,
        message_body: str,
    ) -> dict[str, Any]:
        """Synchronous SMS send via boto3. Run via run_in_executor."""
        client = self._get_boto_client()
        kwargs: dict[str, Any] = {
            "DestinationPhoneNumber": destination_phone,
            "OriginationIdentity": self._origination_identity,
            "MessageBody": message_body,
            "MessageType": self._message_type,
        }
        dlt_params = self._build_destination_country_params()
        if dlt_params:
            kwargs.update(dlt_params)
        return client.send_text_message(**kwargs)  # type: ignore[no-any-return]

    async def send(
        self,
        delivery: NotificationDeliveryModel,
        logical: NotificationLogModel,
    ) -> DeliveryResult:
        """Deliver logical notification as an SMS via AWS End User Messaging SMS v2.

        Never raises unhandled exceptions. Classifies AWS errors as retryable or permanent.
        Credentials are resolved from the ambient AWS credential chain — never logged.
        """
        if not self._enabled:
            logger.debug("SMS delivery %s skipped: adapter disabled", delivery.delivery_id)
            return DeliveryResult(
                success=False,
                error_message="SMS channel adapter is disabled",
                is_retryable=False,
                error_details={"reason": "CHANNEL_DISABLED"},
            )

        if not self._origination_identity:
            return DeliveryResult(
                success=False,
                error_message="SMS origination_identity not configured",
                is_retryable=False,
                error_details={"reason": "UNCONFIGURED_ORIGINATION_IDENTITY"},
            )

        # Recipient: delivery record takes priority (allows per-delivery overrides)
        destination_phone = delivery.recipient
        if not destination_phone or destination_phone == "default":
            destination_phone = self._recipient_phone
        if not destination_phone:
            return DeliveryResult(
                success=False,
                error_message="SMS recipient phone number not configured",
                is_retryable=False,
                error_details={"reason": "UNCONFIGURED_RECIPIENT"},
            )

        await self._apply_rate_limit()

        message_body = _format_sms_text(logical)

        try:
            loop = asyncio.get_running_loop()
            response: dict[str, Any] = await asyncio.wait_for(
                loop.run_in_executor(
                    _SMS_EXECUTOR,
                    self._send_sms_sync,
                    destination_phone,
                    message_body,
                ),
                timeout=self._timeout_seconds,
            )
            message_id = response.get("MessageId") or ""
            logger.info(
                "SMS delivery succeeded: delivery_id=%s notification_id=%s message_id=%s",
                delivery.delivery_id,
                logical.notification_id,
                message_id,
            )
            return DeliveryResult(
                success=True,
                provider_message_id=message_id,
                is_retryable=False,
            )

        except TimeoutError:
            logger.warning(
                "SMS delivery timeout (%.1fs) for delivery %s",
                self._timeout_seconds,
                delivery.delivery_id,
            )
            return DeliveryResult(
                success=False,
                error_message=f"SMS delivery timed out after {self._timeout_seconds}s",
                is_retryable=True,
                error_details={"reason": "TIMEOUT"},
            )

        except Exception as exc:  # noqa: BLE001
            # Classify botocore.exceptions.ClientError by AWS error code.
            # ClientError inherits from Exception. We catch broadly here to
            # handle all provider errors from run_in_executor; the code path
            # inspects exc.response["Error"]["Code"] to classify retryable vs
            # permanent before logging — no credential details are ever logged.
            error_code: str | None = None
            error_message_str: str = str(exc)
            try:
                # ClientError stores the code at exc.response["Error"]["Code"]
                error_code = exc.response["Error"]["Code"]  # type: ignore[attr-defined]
                error_message_str = exc.response["Error"]["Message"]  # type: ignore[attr-defined]
            except (AttributeError, KeyError, TypeError):
                pass

            is_permanent = (
                error_code in _PERMANENT_AWS_ERRORS
                if error_code
                else False
            )
            is_retryable = not is_permanent

            if is_permanent:
                logger.error(
                    "SMS delivery permanent error (code=%s) for delivery %s: %s",
                    error_code,
                    delivery.delivery_id,
                    error_message_str,
                )
            else:
                logger.warning(
                    "SMS delivery transient error (code=%s) for delivery %s: %s",
                    error_code or "UNKNOWN",
                    delivery.delivery_id,
                    error_message_str,
                )

            # Do NOT include raw exception str in error_details to avoid leaking
            # any credential-adjacent context that may appear in boto3 exceptions.
            return DeliveryResult(
                success=False,
                error_message=f"SMS send failed: {error_message_str}",
                is_retryable=is_retryable,
                error_details={
                    "aws_error_code": error_code or "UNKNOWN",
                    "reason": "PERMANENT_PROVIDER_ERROR" if is_permanent else "TRANSIENT_PROVIDER_ERROR",
                },
            )
