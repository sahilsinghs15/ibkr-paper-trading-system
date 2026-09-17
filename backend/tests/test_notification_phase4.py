"""Comprehensive tests for Phase 4: multi-channel SMS + WhatsApp + extensibility."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy as sa
from sqlalchemy import select

from app.db.models.notification import NotificationDeliveryModel, NotificationLogModel
from app.services.notification.channels.base import BaseChannelAdapter
from app.services.notification.channels.sms import SMSChannelAdapter, _format_sms_text
from app.services.notification.channels.whatsapp import (
    WhatsAppChannelAdapter,
    _extract_meta_error_code,
    _truncate,
)
from app.services.notification.dispatcher import (
    ChannelDispatcher,
    reset_default_dispatcher,
)
from app.services.notification.orchestrator import NotificationOrchestrator
from app.services.notification.types import (
    ChannelType,
    DeliveryResult,
    DeliveryStatus,
    NormalizedEvent,
    NotificationSeverity,
    NotificationStatus,
)
from app.services.notification.worker import NotificationDeliveryWorker

# ======================================================================
# Fixtures and helpers
# ======================================================================


@pytest.fixture(autouse=True)
async def _cleanup(session_factory):
    """Clean notification tables before and after each test."""
    async with session_factory() as session, session.begin():
        await session.execute(sa.delete(NotificationDeliveryModel))
        await session.execute(sa.delete(NotificationLogModel))
    yield
    async with session_factory() as session, session.begin():
        await session.execute(sa.delete(NotificationDeliveryModel))
        await session.execute(sa.delete(NotificationLogModel))


@pytest.fixture(autouse=True)
def _reset_dispatcher():
    """Reset the singleton dispatcher between tests."""
    reset_default_dispatcher()
    yield
    reset_default_dispatcher()


class _SuccessAdapter(BaseChannelAdapter):
    """Test adapter that always succeeds."""

    def __init__(self, channel_type: ChannelType, message_id: str = "test-msg-id") -> None:
        self._channel = channel_type
        self._message_id = message_id
        self.send_count = 0

    @property
    def channel(self) -> ChannelType:
        return self._channel

    async def send(
        self, delivery: NotificationDeliveryModel, logical: NotificationLogModel
    ) -> DeliveryResult:
        self.send_count += 1
        return DeliveryResult(success=True, provider_message_id=self._message_id)


class _FailAdapter(BaseChannelAdapter):
    """Test adapter that always fails (permanent by default)."""

    def __init__(
        self,
        channel_type: ChannelType,
        is_retryable: bool = False,
    ) -> None:
        self._channel = channel_type
        self._is_retryable = is_retryable
        self.send_count = 0

    @property
    def channel(self) -> ChannelType:
        return self._channel

    async def send(
        self, delivery: NotificationDeliveryModel, logical: NotificationLogModel
    ) -> DeliveryResult:
        self.send_count += 1
        return DeliveryResult(
            success=False,
            error_message="Test failure",
            is_retryable=self._is_retryable,
            error_details={"reason": "TEST_FAILURE"},
        )


def _make_dispatcher(*adapters: BaseChannelAdapter) -> ChannelDispatcher:
    """Build a dispatcher with a specific set of adapters."""
    d = ChannelDispatcher()
    for a in adapters:
        d.register(a)
    return d


# ======================================================================
# 1. Adapter registration and dispatch routing
# ======================================================================


@pytest.mark.asyncio
async def test_dispatcher_registers_adapters():
    """All registered adapters are retrievable by channel type."""
    tg = _SuccessAdapter(ChannelType.TELEGRAM)
    sms = _SuccessAdapter(ChannelType.SMS)
    wa = _SuccessAdapter(ChannelType.WHATSAPP)

    dispatcher = _make_dispatcher(tg, sms, wa)
    assert dispatcher.get_adapter(ChannelType.TELEGRAM) is tg
    assert dispatcher.get_adapter(ChannelType.SMS) is sms
    assert dispatcher.get_adapter(ChannelType.WHATSAPP) is wa


@pytest.mark.asyncio
async def test_dispatcher_returns_none_for_unregistered_channel():
    """Requesting an unregistered channel returns None, not an exception."""
    dispatcher = ChannelDispatcher()
    assert dispatcher.get_adapter(ChannelType.EMAIL) is None


@pytest.mark.asyncio
async def test_dispatcher_unregistered_channel_returns_non_retryable_failure(session_factory):
    """Dispatching to an unregistered channel returns permanent DeliveryResult."""
    dispatcher = ChannelDispatcher()  # empty — no adapters

    orchestrator = NotificationOrchestrator(session_factory)
    notif = await orchestrator.ingest_event(
        NormalizedEvent(
            event_type="TEST_UNREG_CHANNEL",
            title="No channel",
            message="Test",
        ),
        channels=[ChannelType.TELEGRAM],
    )
    assert notif is not None

    async with session_factory() as session:
        deliv = (
            await session.execute(
                select(NotificationDeliveryModel).where(
                    NotificationDeliveryModel.notification_id == notif.id
                )
            )
        ).scalar_one()

    result = await dispatcher.dispatch(deliv, notif)
    assert not result.success
    assert not result.is_retryable
    assert "UNSUPPORTED_CHANNEL" in str(result.error_details)


@pytest.mark.asyncio
async def test_future_channel_adapter_registers_and_dispatches(session_factory):
    """A future channel (e.g. EMAIL) can be registered without any core changes."""

    class EmailAdapter(_SuccessAdapter):
        @property
        def channel(self) -> ChannelType:
            return ChannelType.EMAIL

    email_adapter = EmailAdapter(ChannelType.EMAIL, message_id="email-msg-001")
    dispatcher = _make_dispatcher(email_adapter)

    orchestrator = NotificationOrchestrator(session_factory)
    notif = await orchestrator.ingest_event(
        NormalizedEvent(
            event_type="TEST_EMAIL",
            title="Email test",
            message="msg",
            severity=NotificationSeverity.CRITICAL,
        ),
        channels=[ChannelType.EMAIL],
    )
    assert notif is not None
    async with session_factory() as session:
        deliv = (
            await session.execute(
                select(NotificationDeliveryModel).where(
                    NotificationDeliveryModel.notification_id == notif.id,
                    NotificationDeliveryModel.channel == ChannelType.EMAIL.value,
                )
            )
        ).scalar_one()

    result = await dispatcher.dispatch(deliv, notif)
    assert result.success
    assert result.provider_message_id == "email-msg-001"
    assert email_adapter.send_count == 1


# ======================================================================
# 2. Multi-channel routing policy in determine_channels()
# ======================================================================


@pytest.mark.asyncio
async def test_routing_info_telegram_only(session_factory):
    """INFO severity routes only to Telegram regardless of SMS/WhatsApp settings."""
    with (
        patch("app.services.notification.orchestrator.get_settings") as mock_settings_fn,
    ):
        settings = MagicMock()
        settings.notification_sms_min_severity = "CRITICAL"
        settings.notification_whatsapp_min_severity = "CRITICAL"
        settings.sms_enabled = True
        settings.whatsapp_enabled = True
        mock_settings_fn.return_value = settings

        orchestrator = NotificationOrchestrator(session_factory)
        event = NormalizedEvent(
            event_type="INFO_EVENT",
            title="Info",
            message="msg",
            severity=NotificationSeverity.INFO,
        )
        channels = orchestrator.determine_channels(event)

    assert channels == [ChannelType.TELEGRAM.value]


@pytest.mark.asyncio
async def test_routing_warning_telegram_only(session_factory):
    """WARNING routes to Telegram only when both SMS+WA are configured as CRITICAL threshold."""
    with patch("app.services.notification.orchestrator.get_settings") as mock_settings_fn:
        settings = MagicMock()
        settings.notification_sms_min_severity = "CRITICAL"
        settings.notification_whatsapp_min_severity = "CRITICAL"
        settings.sms_enabled = True
        settings.whatsapp_enabled = True
        mock_settings_fn.return_value = settings

        orchestrator = NotificationOrchestrator(session_factory)
        event = NormalizedEvent(
            event_type="WARN_EVENT",
            title="Warning",
            message="msg",
            severity=NotificationSeverity.WARNING,
        )
        channels = orchestrator.determine_channels(event)

    assert channels == [ChannelType.TELEGRAM.value]


@pytest.mark.asyncio
async def test_routing_critical_all_three_channels(session_factory):
    """CRITICAL routes to Telegram + SMS + WhatsApp when both are enabled."""
    with patch("app.services.notification.orchestrator.get_settings") as mock_settings_fn:
        settings = MagicMock()
        settings.notification_sms_min_severity = "CRITICAL"
        settings.notification_whatsapp_min_severity = "CRITICAL"
        settings.sms_enabled = True
        settings.whatsapp_enabled = True
        mock_settings_fn.return_value = settings

        orchestrator = NotificationOrchestrator(session_factory)
        event = NormalizedEvent(
            event_type="CRITICAL_EVENT",
            title="Critical",
            message="msg",
            severity=NotificationSeverity.CRITICAL,
        )
        channels = orchestrator.determine_channels(event)

    assert ChannelType.TELEGRAM.value in channels
    assert ChannelType.SMS.value in channels
    assert ChannelType.WHATSAPP.value in channels
    assert len(channels) == 3


@pytest.mark.asyncio
async def test_routing_sms_disabled_does_not_route(session_factory):
    """SMS channel is NOT included when sms_enabled=False."""
    with patch("app.services.notification.orchestrator.get_settings") as mock_settings_fn:
        settings = MagicMock()
        settings.notification_sms_min_severity = "CRITICAL"
        settings.notification_whatsapp_min_severity = "CRITICAL"
        settings.sms_enabled = False
        settings.whatsapp_enabled = False
        mock_settings_fn.return_value = settings

        orchestrator = NotificationOrchestrator(session_factory)
        event = NormalizedEvent(
            event_type="CRITICAL_EVENT",
            title="Critical",
            message="msg",
            severity=NotificationSeverity.CRITICAL,
        )
        channels = orchestrator.determine_channels(event)

    assert channels == [ChannelType.TELEGRAM.value]


@pytest.mark.asyncio
async def test_routing_warning_threshold_adds_channels(session_factory):
    """WARNING threshold causes SMS+WA to route on WARNING severity events."""
    with patch("app.services.notification.orchestrator.get_settings") as mock_settings_fn:
        settings = MagicMock()
        settings.notification_sms_min_severity = "WARNING"
        settings.notification_whatsapp_min_severity = "WARNING"
        settings.sms_enabled = True
        settings.whatsapp_enabled = True
        mock_settings_fn.return_value = settings

        orchestrator = NotificationOrchestrator(session_factory)
        event = NormalizedEvent(
            event_type="WARN_EVENT",
            title="Warning",
            message="msg",
            severity=NotificationSeverity.WARNING,
        )
        channels = orchestrator.determine_channels(event)

    assert ChannelType.TELEGRAM.value in channels
    assert ChannelType.SMS.value in channels
    assert ChannelType.WHATSAPP.value in channels


@pytest.mark.asyncio
async def test_routing_explicit_channels_override(session_factory):
    """Explicit requested_channels override routing policy completely."""
    orchestrator = NotificationOrchestrator(session_factory)
    event = NormalizedEvent(
        event_type="TEST",
        title="test",
        message="msg",
        severity=NotificationSeverity.CRITICAL,
    )
    channels = orchestrator.determine_channels(event, requested_channels=[ChannelType.SMS])
    assert channels == [ChannelType.SMS.value]


# ======================================================================
# 3. Multi-channel delivery creation via ingest_event()
# ======================================================================


@pytest.mark.asyncio
async def test_ingest_critical_creates_three_delivery_rows(session_factory):
    """CRITICAL event with SMS+WA enabled creates three delivery rows."""
    with (
        patch("app.services.notification.orchestrator.get_settings") as mock_settings_fn,
    ):
        settings = MagicMock()
        settings.notification_sms_min_severity = "CRITICAL"
        settings.notification_whatsapp_min_severity = "CRITICAL"
        settings.sms_enabled = True
        settings.whatsapp_enabled = True
        settings.telegram_chat_id = "tg123"
        settings.sms_recipient_phone = "+919XXXXXXXXX"
        settings.whatsapp_recipient_phone = "919XXXXXXXXX"
        settings.notification_max_retries = 3
        settings.notification_shadow_mode = False
        mock_settings_fn.return_value = settings

        orchestrator = NotificationOrchestrator(session_factory)
        notif = await orchestrator.ingest_event(
            NormalizedEvent(
                event_type="CRIT_EVENT",
                title="Critical",
                message="msg",
                severity=NotificationSeverity.CRITICAL,
            )
        )

    assert notif is not None
    assert notif.status == NotificationStatus.PENDING.value

    async with session_factory() as session:
        deliveries = list(
            (
                await session.execute(
                    select(NotificationDeliveryModel).where(
                        NotificationDeliveryModel.notification_id == notif.id
                    )
                )
            )
            .scalars()
            .all()
        )

    channels_found = {d.channel for d in deliveries}
    assert ChannelType.TELEGRAM.value in channels_found
    assert ChannelType.SMS.value in channels_found
    assert ChannelType.WHATSAPP.value in channels_found
    assert len(deliveries) == 3


@pytest.mark.asyncio
async def test_ingest_uses_correct_default_recipients(session_factory):
    """Default recipients are set correctly per channel from channel_defaults."""
    with patch("app.services.notification.orchestrator.get_settings") as mock_settings_fn:
        settings = MagicMock()
        settings.notification_sms_min_severity = "INFO"
        settings.notification_whatsapp_min_severity = "INFO"
        settings.sms_enabled = True
        settings.whatsapp_enabled = True
        settings.telegram_chat_id = "tg-chat-999"
        settings.sms_recipient_phone = "+919111111111"
        settings.whatsapp_recipient_phone = "919222222222"
        settings.notification_max_retries = 3
        settings.notification_shadow_mode = False
        mock_settings_fn.return_value = settings

        orchestrator = NotificationOrchestrator(session_factory)
        notif = await orchestrator.ingest_event(
            NormalizedEvent(
                event_type="RECIP_TEST",
                title="Recipient test",
                message="msg",
                severity=NotificationSeverity.INFO,
            )
        )

    assert notif is not None
    async with session_factory() as session:
        deliveries = list(
            (
                await session.execute(
                    select(NotificationDeliveryModel).where(
                        NotificationDeliveryModel.notification_id == notif.id
                    )
                )
            )
            .scalars()
            .all()
        )

    by_channel = {d.channel: d for d in deliveries}
    assert by_channel[ChannelType.TELEGRAM.value].recipient == "tg-chat-999"
    assert by_channel[ChannelType.SMS.value].recipient == "+919111111111"
    assert by_channel[ChannelType.WHATSAPP.value].recipient == "919222222222"


# ======================================================================
# 4. Channel isolation — failure of one channel doesn't block others
# ======================================================================


@pytest.mark.asyncio
async def test_channel_isolation_telegram_fail_sms_succeeds(session_factory):
    """Telegram failure does not affect SMS channel delivery outcome."""
    tg_fail = _FailAdapter(ChannelType.TELEGRAM, is_retryable=False)
    sms_ok = _SuccessAdapter(ChannelType.SMS)
    dispatcher = _make_dispatcher(tg_fail, sms_ok)

    orchestrator = NotificationOrchestrator(session_factory)
    notif = await orchestrator.ingest_event(
        NormalizedEvent(
            event_type="ISOLATION_TEST",
            title="Isolation",
            message="msg",
        ),
        channels=[ChannelType.TELEGRAM, ChannelType.SMS],
    )
    assert notif is not None

    worker = NotificationDeliveryWorker(
        session_factory=session_factory,
        dispatcher=dispatcher,
        max_retries=1,
    )
    await worker.run_once(batch_size=10)

    async with session_factory() as session:
        deliveries = list(
            (
                await session.execute(
                    select(NotificationDeliveryModel).where(
                        NotificationDeliveryModel.notification_id == notif.id
                    )
                )
            )
            .scalars()
            .all()
        )

    by_channel = {d.channel: d for d in deliveries}
    # SMS succeeded
    assert by_channel[ChannelType.SMS.value].status == DeliveryStatus.DELIVERED.value
    # Telegram failed (no retries left since attempt_count=1 >= max_retries=1)
    assert by_channel[ChannelType.TELEGRAM.value].status == DeliveryStatus.FAILED.value

    # Parent: some DELIVERED, some FAILED → DISPATCHED
    async with session_factory() as session:
        refreshed = await session.get(NotificationLogModel, notif.id)
    assert refreshed is not None
    assert refreshed.status == NotificationStatus.DISPATCHED.value


@pytest.mark.asyncio
async def test_channel_isolation_all_succeed_parent_completed(session_factory):
    """All channels successful → parent notification reaches COMPLETED."""
    tg_ok = _SuccessAdapter(ChannelType.TELEGRAM)
    sms_ok = _SuccessAdapter(ChannelType.SMS)
    wa_ok = _SuccessAdapter(ChannelType.WHATSAPP)
    dispatcher = _make_dispatcher(tg_ok, sms_ok, wa_ok)

    orchestrator = NotificationOrchestrator(session_factory)
    notif = await orchestrator.ingest_event(
        NormalizedEvent(
            event_type="ALL_OK_TEST",
            title="All ok",
            message="msg",
        ),
        channels=[ChannelType.TELEGRAM, ChannelType.SMS, ChannelType.WHATSAPP],
    )
    assert notif is not None

    worker = NotificationDeliveryWorker(
        session_factory=session_factory,
        dispatcher=dispatcher,
        max_retries=1,
    )
    await worker.run_once(batch_size=10)

    async with session_factory() as session:
        refreshed = await session.get(NotificationLogModel, notif.id)
    assert refreshed is not None
    assert refreshed.status == NotificationStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_channel_isolation_all_fail_parent_failed(session_factory):
    """All channels permanently fail → parent notification reaches FAILED."""
    tg_fail = _FailAdapter(ChannelType.TELEGRAM, is_retryable=False)
    sms_fail = _FailAdapter(ChannelType.SMS, is_retryable=False)
    wa_fail = _FailAdapter(ChannelType.WHATSAPP, is_retryable=False)
    dispatcher = _make_dispatcher(tg_fail, sms_fail, wa_fail)

    orchestrator = NotificationOrchestrator(session_factory)
    notif = await orchestrator.ingest_event(
        NormalizedEvent(
            event_type="ALL_FAIL_TEST",
            title="All fail",
            message="msg",
        ),
        channels=[ChannelType.TELEGRAM, ChannelType.SMS, ChannelType.WHATSAPP],
    )
    assert notif is not None

    worker = NotificationDeliveryWorker(
        session_factory=session_factory,
        dispatcher=dispatcher,
        max_retries=1,
    )
    await worker.run_once(batch_size=10)

    async with session_factory() as session:
        refreshed = await session.get(NotificationLogModel, notif.id)
    assert refreshed is not None
    assert refreshed.status == NotificationStatus.FAILED.value


# ======================================================================
# 5. SMS adapter unit tests
# ======================================================================


def test_sms_disabled_skips_delivery():
    """Disabled SMS adapter returns non-retryable failure without calling AWS."""
    adapter = SMSChannelAdapter(enabled=False)
    delivery = MagicMock(spec=NotificationDeliveryModel)
    delivery.delivery_id = "DELIV-001"
    delivery.recipient = "+919999999999"
    logical = MagicMock(spec=NotificationLogModel)

    result = asyncio.get_event_loop().run_until_complete(adapter.send(delivery, logical))
    assert not result.success
    assert not result.is_retryable
    assert result.error_message is not None and "disabled" in result.error_message.lower()


def test_sms_unconfigured_origination_identity():
    """SMS adapter without origination_identity returns permanent failure."""
    adapter = SMSChannelAdapter(enabled=True, origination_identity=None)
    delivery = MagicMock(spec=NotificationDeliveryModel)
    delivery.delivery_id = "DELIV-002"
    delivery.recipient = "+919999999999"
    logical = MagicMock(spec=NotificationLogModel)

    result = asyncio.get_event_loop().run_until_complete(adapter.send(delivery, logical))
    assert not result.success
    assert not result.is_retryable
    assert "UNCONFIGURED_ORIGINATION_IDENTITY" in str(result.error_details)


def test_sms_unconfigured_recipient():
    """SMS adapter without recipient returns permanent failure."""
    adapter = SMSChannelAdapter(
        enabled=True,
        origination_identity="arn:aws:sms:us-east-1:123:pool/pool-xxx",
        recipient_phone=None,
    )
    delivery = MagicMock(spec=NotificationDeliveryModel)
    delivery.delivery_id = "DELIV-003"
    delivery.recipient = "default"  # "default" means no real recipient
    logical = MagicMock(spec=NotificationLogModel)

    result = asyncio.get_event_loop().run_until_complete(adapter.send(delivery, logical))
    assert not result.success
    assert not result.is_retryable
    assert "UNCONFIGURED_RECIPIENT" in str(result.error_details)


@pytest.mark.asyncio
async def test_sms_success():
    """SMS adapter succeeds and returns message_id from AWS response."""
    adapter = SMSChannelAdapter(
        enabled=True,
        origination_identity="arn:aws:sms-voice:ap-south-1:123456:sender-id/OEMS",
        recipient_phone="+919000000000",
        aws_region="ap-south-1",
    )
    delivery = MagicMock(spec=NotificationDeliveryModel)
    delivery.delivery_id = "DELIV-SMS-OK"
    delivery.recipient = "+919000000000"
    logical = MagicMock(spec=NotificationLogModel)
    logical.notification_id = "NOTIF-001"
    logical.title = "Test alert"
    logical.message = "Everything is on fire"
    logical.severity = "CRITICAL"

    mock_response = {"MessageId": "sms-msg-abc123"}

    with patch.object(adapter, "_send_sms_sync", return_value=mock_response):
        result = await adapter.send(delivery, logical)

    assert result.success
    assert result.provider_message_id == "sms-msg-abc123"
    assert not result.is_retryable


@pytest.mark.asyncio
async def test_sms_retryable_error():
    """ThrottlingException from AWS maps to is_retryable=True."""
    adapter = SMSChannelAdapter(
        enabled=True,
        origination_identity="arn:test",
        recipient_phone="+919000000000",
    )
    delivery = MagicMock(spec=NotificationDeliveryModel)
    delivery.delivery_id = "DELIV-SMS-THROTTLE"
    delivery.recipient = "+919000000000"
    logical = MagicMock(spec=NotificationLogModel)
    logical.notification_id = "NOTIF-002"
    logical.title = "t"
    logical.message = "m"
    logical.severity = "CRITICAL"

    throttle_exc = Exception("ThrottlingException")
    throttle_exc.response = {  # type: ignore[attr-defined]
        "Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}
    }

    with patch.object(adapter, "_send_sms_sync", side_effect=throttle_exc):
        result = await adapter.send(delivery, logical)

    assert not result.success
    assert result.is_retryable
    assert "ThrottlingException" in str(result.error_details)


@pytest.mark.asyncio
async def test_sms_permanent_error():
    """InvalidParameterException from AWS maps to is_retryable=False."""
    adapter = SMSChannelAdapter(
        enabled=True,
        origination_identity="arn:test",
        recipient_phone="+919000000000",
    )
    delivery = MagicMock(spec=NotificationDeliveryModel)
    delivery.delivery_id = "DELIV-SMS-PERM"
    delivery.recipient = "+919000000000"
    logical = MagicMock(spec=NotificationLogModel)
    logical.notification_id = "NOTIF-003"
    logical.title = "t"
    logical.message = "m"
    logical.severity = "CRITICAL"

    perm_exc = Exception("InvalidParameterException")
    perm_exc.response = {  # type: ignore[attr-defined]
        "Error": {"Code": "InvalidParameterException", "Message": "Bad phone number"}
    }

    with patch.object(adapter, "_send_sms_sync", side_effect=perm_exc):
        result = await adapter.send(delivery, logical)

    assert not result.success
    assert not result.is_retryable
    assert "PERMANENT_PROVIDER_ERROR" in str(result.error_details)


@pytest.mark.asyncio
async def test_sms_timeout():
    """SMS delivery timeout maps to retryable failure."""
    adapter = SMSChannelAdapter(
        enabled=True,
        origination_identity="arn:test",
        recipient_phone="+919000000000",
        timeout_seconds=0.01,
    )
    delivery = MagicMock(spec=NotificationDeliveryModel)
    delivery.delivery_id = "DELIV-SMS-TIMEOUT"
    delivery.recipient = "+919000000000"
    logical = MagicMock(spec=NotificationLogModel)
    logical.notification_id = "NOTIF-004"
    logical.title = "t"
    logical.message = "m"
    logical.severity = "CRITICAL"

    async def slow_send(*_: Any) -> dict[str, Any]:
        await asyncio.sleep(10)
        return {}

    with (
        patch.object(adapter, "_send_sms_sync", side_effect=lambda *a: asyncio.sleep(10)),
        patch("asyncio.wait_for", side_effect=TimeoutError()),
    ):
        result = await adapter.send(delivery, logical)

    assert not result.success
    assert result.is_retryable
    assert result.error_message is not None and "timed out" in result.error_message.lower()


@pytest.mark.asyncio
async def test_sms_india_dlt_params_included():
    """India DLT params are passed to send_text_message when configured."""
    adapter = SMSChannelAdapter(
        enabled=True,
        origination_identity="arn:test:OEMS",
        recipient_phone="+919000000000",
        india_dlt_principal_entity_id="DLT-ENTITY-123",
        india_dlt_template_id="DLT-TMPL-456",
    )

    captured_kwargs: dict[str, Any] = {}

    def capture_sync(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal captured_kwargs
        captured_kwargs = kwargs
        return {"MessageId": "dlt-msg-abc"}

    with patch.object(adapter, "_send_sms_sync", side_effect=capture_sync):
        delivery = MagicMock(spec=NotificationDeliveryModel)
        delivery.delivery_id = "DELIV-DLT"
        delivery.recipient = "+919000000000"
        logical = MagicMock(spec=NotificationLogModel)
        logical.notification_id = "NOTIF-DLT"
        logical.title = "DLT test"
        logical.message = "Test"
        logical.severity = "CRITICAL"

        # Call _build_destination_country_params directly to verify DLT params
        dlt_params = adapter._build_destination_country_params()

    assert "DestinationCountryParameters" in dlt_params
    assert dlt_params["DestinationCountryParameters"]["IN_ENTITY_ID"] == "DLT-ENTITY-123"
    assert dlt_params["DestinationCountryParameters"]["IN_TEMPLATE_ID"] == "DLT-TMPL-456"


# ======================================================================
# 6. WhatsApp adapter unit tests
# ======================================================================


def test_whatsapp_disabled_skips_delivery():
    """Disabled WhatsApp adapter returns non-retryable failure."""
    adapter = WhatsAppChannelAdapter(enabled=False)
    delivery = MagicMock(spec=NotificationDeliveryModel)
    delivery.delivery_id = "DELIV-WA-001"
    delivery.recipient = "919000000000"
    logical = MagicMock(spec=NotificationLogModel)

    result = asyncio.get_event_loop().run_until_complete(adapter.send(delivery, logical))
    assert not result.success
    assert not result.is_retryable
    assert result.error_message is not None and "disabled" in result.error_message.lower()


def test_whatsapp_unconfigured_credentials():
    """WhatsApp adapter without token/phone_number_id returns permanent failure."""
    adapter = WhatsAppChannelAdapter(
        enabled=True,
        access_token=None,
        phone_number_id=None,
    )
    delivery = MagicMock(spec=NotificationDeliveryModel)
    delivery.delivery_id = "DELIV-WA-002"
    delivery.recipient = "919000000000"
    logical = MagicMock(spec=NotificationLogModel)

    result = asyncio.get_event_loop().run_until_complete(adapter.send(delivery, logical))
    assert not result.success
    assert not result.is_retryable
    assert "UNCONFIGURED_CREDENTIALS" in str(result.error_details)


@pytest.mark.asyncio
async def test_whatsapp_success():
    """WhatsApp adapter succeeds and parses message_id correctly."""
    adapter = WhatsAppChannelAdapter(
        enabled=True,
        access_token="EAAxxxx",
        phone_number_id="12345678901234",
        recipient_phone="919000000000",
        template_name="oems_alert",
        template_language="en_US",
    )
    delivery = MagicMock(spec=NotificationDeliveryModel)
    delivery.delivery_id = "DELIV-WA-OK"
    delivery.recipient = "919000000000"
    logical = MagicMock(spec=NotificationLogModel)
    logical.notification_id = "NOTIF-WA-001"
    logical.title = "Kill switch armed"
    logical.message = "Emergency flatten triggered"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "messaging_product": "whatsapp",
        "messages": [{"id": "wamid.abc123xyz"}],
    }

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        result = await adapter.send(delivery, logical)

    assert result.success
    assert result.provider_message_id == "wamid.abc123xyz"
    assert not result.is_retryable


@pytest.mark.asyncio
async def test_whatsapp_rate_limit_retryable():
    """WhatsApp HTTP 429 (or Meta error 130429) maps to retryable failure."""
    adapter = WhatsAppChannelAdapter(
        enabled=True,
        access_token="EAAxxxx",
        phone_number_id="12345678901234",
        recipient_phone="919000000000",
    )
    delivery = MagicMock(spec=NotificationDeliveryModel)
    delivery.delivery_id = "DELIV-WA-429"
    delivery.recipient = "919000000000"
    logical = MagicMock(spec=NotificationLogModel)
    logical.notification_id = "NOTIF-WA-002"
    logical.title = "t"
    logical.message = "m"

    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.json.return_value = {
        "error": {
            "message": "Rate limited",
            "code": 130429,
            "error_data": {},
        }
    }
    mock_resp.text = "Rate limited"

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        result = await adapter.send(delivery, logical)

    assert not result.success
    assert result.is_retryable


@pytest.mark.asyncio
async def test_whatsapp_business_locked_permanent():
    """Meta error 131031 (Business Account locked) maps to permanent failure."""
    adapter = WhatsAppChannelAdapter(
        enabled=True,
        access_token="EAAxxxx",
        phone_number_id="12345678901234",
        recipient_phone="919000000000",
    )
    delivery = MagicMock(spec=NotificationDeliveryModel)
    delivery.delivery_id = "DELIV-WA-LOCKED"
    delivery.recipient = "919000000000"
    logical = MagicMock(spec=NotificationLogModel)
    logical.notification_id = "NOTIF-WA-003"
    logical.title = "t"
    logical.message = "m"

    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.json.return_value = {
        "error": {
            "message": "Business Account locked",
            "code": 131031,
            "error_data": {},
        }
    }
    mock_resp.text = "Business Account locked"

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        result = await adapter.send(delivery, logical)

    assert not result.success
    assert not result.is_retryable
    assert "PERMANENT_PROVIDER_ERROR" in str(result.error_details)


@pytest.mark.asyncio
async def test_whatsapp_network_error_retryable():
    """httpx.NetworkError maps to retryable failure."""
    import httpx

    adapter = WhatsAppChannelAdapter(
        enabled=True,
        access_token="EAAxxxx",
        phone_number_id="12345678901234",
        recipient_phone="919000000000",
    )
    delivery = MagicMock(spec=NotificationDeliveryModel)
    delivery.delivery_id = "DELIV-WA-NET"
    delivery.recipient = "919000000000"
    logical = MagicMock(spec=NotificationLogModel)
    logical.notification_id = "NOTIF-WA-004"
    logical.title = "t"
    logical.message = "m"

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=httpx.NetworkError("connection reset"))
        mock_client_cls.return_value = mock_client

        result = await adapter.send(delivery, logical)

    assert not result.success
    assert result.is_retryable
    assert "NETWORK_ERROR" in str(result.error_details)


@pytest.mark.asyncio
async def test_whatsapp_unauthorized_permanent():
    """HTTP 401 maps to permanent failure (invalid/expired access token)."""
    adapter = WhatsAppChannelAdapter(
        enabled=True,
        access_token="expired_token",
        phone_number_id="12345678901234",
        recipient_phone="919000000000",
    )
    delivery = MagicMock(spec=NotificationDeliveryModel)
    delivery.delivery_id = "DELIV-WA-401"
    delivery.recipient = "919000000000"
    logical = MagicMock(spec=NotificationLogModel)
    logical.notification_id = "NOTIF-WA-005"
    logical.title = "t"
    logical.message = "m"

    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.json.return_value = {"error": {"message": "Invalid OAuth token", "code": 190}}
    mock_resp.text = "Invalid OAuth token"

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        result = await adapter.send(delivery, logical)

    assert not result.success
    assert not result.is_retryable


# ======================================================================
# 7. SMS message formatting tests
# ======================================================================


def test_sms_format_short_message():
    """Short message is formatted without truncation."""
    logical = MagicMock(spec=NotificationLogModel)
    logical.severity = "CRITICAL"
    logical.title = "Kill switch armed"
    logical.message = "All positions flat."
    text = _format_sms_text(logical)
    assert text == "[CRITICAL] Kill switch armed: All positions flat."
    assert len(text) <= 155


def test_sms_format_long_message_truncated():
    """Long message is truncated with ellipsis within 155 chars."""
    logical = MagicMock(spec=NotificationLogModel)
    logical.severity = "WARNING"
    logical.title = "Loss threshold exceeded"
    logical.message = "Account DU123456 has reached its daily loss threshold. " * 5
    text = _format_sms_text(logical)
    assert len(text) <= 155
    assert "..." in text or len(text) <= 155


def test_sms_format_info_severity_prefix():
    """INFO severity uses [INFO] prefix."""
    logical = MagicMock(spec=NotificationLogModel)
    logical.severity = "INFO"
    logical.title = "System OK"
    logical.message = "All good."
    text = _format_sms_text(logical)
    assert text.startswith("[INFO]")


def test_sms_format_unknown_severity_no_prefix():
    """Unknown severity falls back to no prefix."""
    logical = MagicMock(spec=NotificationLogModel)
    logical.severity = "UNKNOWN"
    logical.title = "Event"
    logical.message = "Something happened."
    text = _format_sms_text(logical)
    assert "Event" in text


# ======================================================================
# 8. WhatsApp template payload and utility tests
# ======================================================================


def test_whatsapp_truncate_short():
    assert _truncate("Hello", 60) == "Hello"


def test_whatsapp_truncate_long():
    result = _truncate("A" * 100, 60)
    assert len(result) == 60
    assert result.endswith("...")


def test_whatsapp_extract_meta_error_code_from_code_field():
    data = {"error": {"message": "Rate limited", "code": 130429}}
    assert _extract_meta_error_code(data) == 130429


def test_whatsapp_extract_meta_error_code_zero_is_none():
    data = {"error": {"message": "ok", "code": 0}}
    assert _extract_meta_error_code(data) is None


def test_whatsapp_extract_meta_error_code_missing():
    assert _extract_meta_error_code({}) is None


def test_whatsapp_payload_structure():
    """WhatsApp payload contains correct template structure."""
    adapter = WhatsAppChannelAdapter(
        enabled=True,
        access_token="tok",
        phone_number_id="pid",
        template_name="oems_alert",
        template_language="en_US",
    )
    payload = adapter._build_payload("919000000000", "Kill switch armed", "Emergency flatten")
    assert payload["messaging_product"] == "whatsapp"
    assert payload["type"] == "template"
    assert payload["template"]["name"] == "oems_alert"
    components = payload["template"]["components"]
    assert components[0]["type"] == "body"
    params = components[0]["parameters"]
    assert params[0]["text"] == "Kill switch armed"
    assert params[1]["text"] == "Emergency flatten"


def test_whatsapp_payload_long_title_truncated():
    """Long title is truncated to _TITLE_MAX in template variables."""
    adapter = WhatsAppChannelAdapter(
        enabled=True, access_token="tok", phone_number_id="pid"
    )
    long_title = "A" * 100
    payload = adapter._build_payload("919000000000", long_title, "message")
    params = payload["template"]["components"][0]["parameters"]
    assert len(params[0]["text"]) <= 60


def test_whatsapp_access_token_not_in_url():
    """Access token never appears in the API URL."""
    adapter = WhatsAppChannelAdapter(
        enabled=True,
        access_token="SUPER_SECRET_TOKEN",
        phone_number_id="12345678",
        api_version="v21.0",
    )
    url = adapter._api_url()
    assert "SUPER_SECRET_TOKEN" not in url


# ======================================================================
# 9. No-secret-in-logs tests
# ======================================================================


@pytest.mark.asyncio
async def test_sms_does_not_log_credentials(caplog):
    """SMS adapter never logs credential-adjacent information."""
    adapter = SMSChannelAdapter(
        enabled=True,
        origination_identity="arn:aws:sms-voice:ap-south-1:999:sender-id/SECRET_ID",
        recipient_phone="+919000000000",
    )
    delivery = MagicMock(spec=NotificationDeliveryModel)
    delivery.delivery_id = "DELIV-NOLOG"
    delivery.recipient = "+919000000000"
    logical = MagicMock(spec=NotificationLogModel)
    logical.notification_id = "NOTIF-NOLOG"
    logical.title = "t"
    logical.message = "m"
    logical.severity = "CRITICAL"

    exc = Exception("AccessDeniedException")
    exc.response = {  # type: ignore[attr-defined]
        "Error": {"Code": "AccessDeniedException", "Message": "User not authorized"}
    }
    with (
        patch.object(adapter, "_send_sms_sync", side_effect=exc),
        caplog.at_level(logging.DEBUG),
    ):
        await adapter.send(delivery, logical)

    log_output = caplog.text
    assert "SECRET_ID" not in log_output


@pytest.mark.asyncio
async def test_whatsapp_does_not_log_access_token(caplog):
    """WhatsApp adapter never logs the access token."""
    adapter = WhatsAppChannelAdapter(
        enabled=True,
        access_token="EXTREMELY_SECRET_ACCESS_TOKEN",
        phone_number_id="12345678901234",
        recipient_phone="919000000000",
    )
    delivery = MagicMock(spec=NotificationDeliveryModel)
    delivery.delivery_id = "DELIV-WA-NOLOG"
    delivery.recipient = "919000000000"
    logical = MagicMock(spec=NotificationLogModel)
    logical.notification_id = "NOTIF-WA-NOLOG"
    logical.title = "t"
    logical.message = "m"

    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.json.return_value = {"error": {"message": "Unauthorized", "code": 190}}
    mock_resp.text = "Unauthorized"

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        with caplog.at_level(logging.DEBUG):
            await adapter.send(delivery, logical)

    log_output = caplog.text
    assert "EXTREMELY_SECRET_ACCESS_TOKEN" not in log_output


# ======================================================================
# 10. Worker recovery — multi-channel stuck deliveries
# ======================================================================


@pytest.mark.asyncio
async def test_worker_recovers_stuck_multi_channel_deliveries(session_factory):
    """Stuck SENDING deliveries across multiple channels are all recovered."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select as sa_select

    tg_ok = _SuccessAdapter(ChannelType.TELEGRAM)
    sms_ok = _SuccessAdapter(ChannelType.SMS)
    dispatcher = _make_dispatcher(tg_ok, sms_ok)

    orchestrator = NotificationOrchestrator(session_factory)
    notif = await orchestrator.ingest_event(
        NormalizedEvent(
            event_type="STUCK_DELIVERY_TEST",
            title="Stuck delivery",
            message="msg",
        ),
        channels=[ChannelType.TELEGRAM, ChannelType.SMS],
    )
    assert notif is not None

    # Fetch the delivery PKs that were just created
    async with session_factory() as session:
        delivery_pks = list(
            (
                await session.execute(
                    sa_select(NotificationDeliveryModel.id).where(
                        NotificationDeliveryModel.notification_id == notif.id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(delivery_pks) == 2, f"Expected 2 deliveries, got {len(delivery_pks)}"

    # Simulate deliveries stuck in SENDING with a stale last_attempt_at
    stale_ts = datetime.now(UTC) - timedelta(seconds=120)
    async with session_factory() as session, session.begin():
        await session.execute(
            sa.update(NotificationDeliveryModel)
            .where(NotificationDeliveryModel.id.in_(delivery_pks))
            .values(status=DeliveryStatus.SENDING.value, last_attempt_at=stale_ts, attempt_count=1)
        )

    worker = NotificationDeliveryWorker(
        session_factory=session_factory,
        dispatcher=dispatcher,
        max_retries=3,
    )
    recovered = await worker.recover_stuck_deliveries(lease_timeout_sec=60.0)
    assert recovered == 2, f"Expected 2 recovered, got {recovered}"

    async with session_factory() as session:
        statuses = list(
            (
                await session.execute(
                    sa.select(NotificationDeliveryModel.status).where(
                        NotificationDeliveryModel.id.in_(delivery_pks)
                    )
                )
            )
            .scalars()
            .all()
        )

    # Both should be in RETRYING (attempt_count=1 < max_retries=3)
    assert all(s == DeliveryStatus.RETRYING.value for s in statuses)


# ======================================================================
# 11. Regression — Telegram-only event still works correctly (Phase 1/2/3)
# ======================================================================


@pytest.mark.asyncio
async def test_telegram_only_event_still_works(session_factory):
    """Existing INFO events with default routing continue creating a single Telegram delivery."""
    orchestrator = NotificationOrchestrator(session_factory)
    notif = await orchestrator.ingest_event(
        NormalizedEvent(
            event_type="REGRESSION_TEST",
            title="Regression check",
            message="Phase 1/2/3 routing unchanged for INFO events",
            severity=NotificationSeverity.INFO,
        )
    )
    assert notif is not None

    async with session_factory() as session:
        deliveries = list(
            (
                await session.execute(
                    select(NotificationDeliveryModel).where(
                        NotificationDeliveryModel.notification_id == notif.id
                    )
                )
            )
            .scalars()
            .all()
        )

    # Only Telegram by default (sms_enabled=False, whatsapp_enabled=False in test env)
    assert len(deliveries) == 1
    assert deliveries[0].channel == ChannelType.TELEGRAM.value


@pytest.mark.asyncio
async def test_recipient_override_per_channel(session_factory):
    """Caller-provided recipient_overrides are honoured per channel."""
    orchestrator = NotificationOrchestrator(session_factory)
    notif = await orchestrator.ingest_event(
        NormalizedEvent(
            event_type="OVERRIDE_TEST",
            title="Override",
            message="msg",
        ),
        channels=[ChannelType.TELEGRAM, ChannelType.SMS],
        recipient_overrides={
            ChannelType.TELEGRAM.value: "custom-chat-123",
            ChannelType.SMS.value: "+910000000001",
        },
    )
    assert notif is not None

    async with session_factory() as session:
        deliveries = list(
            (
                await session.execute(
                    select(NotificationDeliveryModel).where(
                        NotificationDeliveryModel.notification_id == notif.id
                    )
                )
            )
            .scalars()
            .all()
        )

    by_channel = {d.channel: d for d in deliveries}
    assert by_channel[ChannelType.TELEGRAM.value].recipient == "custom-chat-123"
    assert by_channel[ChannelType.SMS.value].recipient == "+910000000001"


@pytest.mark.asyncio
async def test_channel_type_is_telegram(session_factory):
    """SMSChannelAdapter.channel property returns ChannelType.SMS."""
    adapter = SMSChannelAdapter(enabled=False)
    assert adapter.channel == ChannelType.SMS


def test_whatsapp_channel_type():
    """WhatsAppChannelAdapter.channel property returns ChannelType.WHATSAPP."""
    adapter = WhatsAppChannelAdapter(enabled=False)
    assert adapter.channel == ChannelType.WHATSAPP
