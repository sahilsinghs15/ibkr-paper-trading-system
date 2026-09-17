"""IBKR broker connection lifecycle listener that reports events to NotificationOrchestrator."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from app.core.config import get_settings
from app.services.notification.orchestrator import NotificationOrchestrator
from app.services.notification.types import (
    NormalizedEvent,
    NotificationSeverity,
)

logger = logging.getLogger(__name__)


class BrokerNotificationListener:
    """Listens to TWSClient callbacks and emits normalized broker lifecycle events."""

    def __init__(
        self,
        orchestrator: NotificationOrchestrator,
        client: Any = None,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        startup_aggregator: Any | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._client = client
        self._loop = loop
        self._startup_aggregator = startup_aggregator
        self._lock = threading.Lock()
        # Edge-triggered connection state: only transition on genuine state change
        self._is_connected: bool = True

    def mark_connected(self) -> None:
        """Explicitly mark broker connection state as active."""
        with self._lock:
            self._is_connected = True

    def set_startup_aggregator(self, aggregator: Any | None) -> None:
        """Set or update startup aggregator reference."""
        self._startup_aggregator = aggregator

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind active event loop for scheduling async coroutines from background thread."""
        self._loop = loop

    def _dispatch(self, coro: Any) -> Any:
        """Schedule coroutine thread-safely onto the event loop."""
        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

        if loop is not None and loop.is_running():
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None

            if running is loop:
                return asyncio.create_task(coro)
            else:
                return asyncio.run_coroutine_threadsafe(coro, loop)
        else:
            logger.warning(
                "No running event loop available to dispatch broker notification event"
            )
            if hasattr(coro, "close"):
                coro.close()
            return None

    def on_connection_closed(self) -> Any:
        """Invoked by TWSClient reader thread when connection is lost."""
        with self._lock:
            if not self._is_connected:
                logger.debug(
                    "on_connection_closed received while already disconnected; suppressing duplicate BROKER_LOST"
                )
                return None
            self._is_connected = False
        return self._dispatch(self._handle_connection_closed())

    def on_connection_restored(self) -> Any:
        """Invoked by TWSClient reconnect thread when socket is restored."""
        with self._lock:
            if self._is_connected:
                logger.debug(
                    "on_connection_restored received while already connected; suppressing duplicate"
                )
                return None
            self._is_connected = True
        return self._dispatch(self._handle_connection_restored())

    def on_next_valid_id(self, order_id: int) -> Any:
        """Invoked by TWSClient when initial handshake finishes (nextValidId received)."""
        with self._lock:
            self._is_connected = True
        if self._startup_aggregator is not None and getattr(self._startup_aggregator, "is_window_active", False):
            logger.info(
                "Startup window active; recording ib_login milestone instead of emitting separate alert (next_order_id=%s)",
                order_id,
            )
            self._startup_aggregator.record_component(
                "ib_login",
                is_ready=True,
                detail=f"Authenticated (next_order_id={order_id})",
            )
            self._startup_aggregator.record_component(
                "broker_connection",
                is_ready=True,
                detail="TWS socket connected and authenticated",
            )
            return None
        return self._dispatch(self._handle_login_completed(order_id))

    async def _handle_connection_closed(self) -> None:
        settings = get_settings()
        host = getattr(self._client, "_connect_host", None) or settings.ibkr_host
        port = getattr(self._client, "_connect_port", None) or settings.ibkr_port
        client_id = getattr(self._client, "_connect_client_id", None) or settings.ibkr_client_id

        event = NormalizedEvent(
            event_type="BROKER_LOST",
            title="IBKR Broker Connection Lost",
            message=(
                f"TWS/Gateway socket disconnected on {host}:{port}. "
                "Automatic reconnection initiated."
            ),
            category="BROKER",
            severity=NotificationSeverity.CRITICAL,
            source="tws_client",
            correlation_id="broker_connection",
            details={
                "host": host,
                "port": port,
                "client_id": client_id,
            },
        )
        try:
            await self._orchestrator.ingest_event(event)
        except Exception:
            logger.exception("Failed ingesting BROKER_LOST event into orchestrator")

    async def _handle_connection_restored(self) -> None:
        settings = get_settings()
        host = getattr(self._client, "_connect_host", None) or settings.ibkr_host
        port = getattr(self._client, "_connect_port", None) or settings.ibkr_port
        client_id = getattr(self._client, "_connect_client_id", None) or settings.ibkr_client_id

        event = NormalizedEvent(
            event_type="BROKER_RECONNECTED",
            title="IBKR Broker Connection Restored",
            message=f"TWS/Gateway socket reconnected successfully on {host}:{port}.",
            category="BROKER",
            severity=NotificationSeverity.INFO,
            source="tws_client",
            correlation_id="broker_connection",
            details={
                "host": host,
                "port": port,
                "client_id": client_id,
            },
        )
        try:
            await self._orchestrator.ingest_event(event)
        except Exception:
            logger.exception("Failed ingesting BROKER_RECONNECTED event into orchestrator")

    async def _handle_login_completed(self, order_id: int) -> None:
        settings = get_settings()
        host = getattr(self._client, "_connect_host", None) or settings.ibkr_host
        port = getattr(self._client, "_connect_port", None) or settings.ibkr_port
        client_id = getattr(self._client, "_connect_client_id", None) or settings.ibkr_client_id

        event = NormalizedEvent(
            event_type="IB_LOGIN_COMPLETED",
            title="IB Login Completed Successfully",
            message=f"IBKR TWS/Gateway session authenticated and ready for orders on {host}:{port} (next_order_id={order_id}).",
            category="BROKER",
            severity=NotificationSeverity.INFO,
            source="tws_client",
            correlation_id="broker_connection",
            dedupe_key=f"ib_login_session_{order_id}",
            details={
                "host": host,
                "port": port,
                "client_id": client_id,
                "order_id": order_id,
            },
        )
        try:
            await self._orchestrator.ingest_event(event)
        except Exception:
            logger.exception("Failed ingesting IB_LOGIN_COMPLETED event into orchestrator")
