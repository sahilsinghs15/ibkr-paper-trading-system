"""Unit tests for IBKRMarketDataAdapter callback and subscription lifecycle."""

import logging
from datetime import UTC
from decimal import Decimal
from unittest import mock

import pytest

from app.broker.ibkr.tws_client import TWSClient
from app.core.config import Settings
from app.market_data.ibkr_market_data import IBKRMarketDataAdapter
from app.models.market_data import MarketDataEvent


class TestIBKRMarketData:
    def test_initial_market_data_state(self) -> None:
        """Verify the adapter starts in an inactive subscription state."""
        client = mock.Mock(spec=TWSClient)
        settings = Settings()
        adapter = IBKRMarketDataAdapter(client, settings)

        assert adapter._active_req_id is None
        assert adapter._active_symbol is None
        assert adapter.queue_size() == 0

    def test_valid_contract_construction(self) -> None:
        """Verify the built contract fields match the configuration settings."""
        client = mock.Mock(spec=TWSClient)
        settings = Settings(
            ibkr_market_data_symbol="AAPL",
            ibkr_market_data_sec_type="STK",
            ibkr_market_data_exchange="SMART",
            ibkr_market_data_currency="USD",
            ibkr_market_data_primary_exchange="NASDAQ",
        )
        adapter = IBKRMarketDataAdapter(client, settings)
        contract = adapter._create_contract()

        assert contract.symbol == "AAPL"
        assert contract.secType == "STK"
        assert contract.exchange == "SMART"
        assert contract.currency == "USD"
        assert contract.primaryExch == "NASDAQ"

    def test_market_data_request(self) -> None:
        """Verify that requesting market data calls TWS API appropriately."""
        client = mock.Mock(spec=TWSClient)
        client.is_connected.return_value = True

        settings = Settings()
        adapter = IBKRMarketDataAdapter(client, settings)

        req_id = adapter.request_market_data()

        # req_id must be a valid integer
        assert isinstance(req_id, int)
        assert adapter._active_req_id == req_id
        assert adapter._active_symbol == settings.ibkr_market_data_symbol

        # Verify TWS client EClient methods are invoked correctly
        client.reqMarketDataType.assert_called_once_with(settings.ibkr_market_data_type)
        client.reqMktData.assert_called_once()
        call_args = client.reqMktData.call_args[0]
        assert call_args[0] == req_id
        contract = call_args[1]
        assert contract.symbol == settings.ibkr_market_data_symbol

    def test_market_data_request_fails_if_not_connected(self) -> None:
        """Verify market data request raises error if client is offline."""
        client = mock.Mock(spec=TWSClient)
        client.is_connected.return_value = False

        settings = Settings()
        adapter = IBKRMarketDataAdapter(client, settings)

        with pytest.raises(RuntimeError, match="TWS client is not connected"):
            adapter.request_market_data()

    def test_duplicate_subscription_behavior(self) -> None:
        """Verify that requesting market data twice is idempotent."""
        client = mock.Mock(spec=TWSClient)
        client.is_connected.return_value = True

        settings = Settings()
        adapter = IBKRMarketDataAdapter(client, settings)

        req_id1 = adapter.request_market_data()
        req_id2 = adapter.request_market_data()

        assert req_id1 == req_id2
        assert client.reqMktData.call_count == 1

    def test_tick_price_normalization(self) -> None:
        """Verify LAST traded price is normalized and enqueued properly."""
        client = mock.Mock(spec=TWSClient)
        client.is_connected.return_value = True

        settings = Settings()
        adapter = IBKRMarketDataAdapter(client, settings)
        req_id = adapter.request_market_data()

        # Callback with tickType 4 (LAST)
        adapter.on_tick_price(req_id, 4, 150.25)

        # Flushed when price changed or on cancellation
        assert adapter.queue_size() == 0
        adapter.cancel_market_data()
        assert adapter.queue_size() == 1
        event = adapter.get_event()
        assert event is not None
        assert isinstance(event, MarketDataEvent)
        assert event.price == Decimal("150.25")
        assert event.volume == 0
        assert event.timestamp.tzinfo == UTC

    def test_tick_price_normalization_delayed(self) -> None:
        """Verify DELAYED_LAST traded price is normalized and enqueued properly."""
        client = mock.Mock(spec=TWSClient)
        client.is_connected.return_value = True

        settings = Settings()
        adapter = IBKRMarketDataAdapter(client, settings)
        req_id = adapter.request_market_data()

        # Callback with tickType 68 (DELAYED_LAST)
        adapter.on_tick_price(req_id, 68, 150.25)

        assert adapter.queue_size() == 0
        adapter.cancel_market_data()
        assert adapter.queue_size() == 1
        event = adapter.get_event()
        assert event is not None
        assert event.price == Decimal("150.25")

    def test_tick_price_ignores_non_associated_req_id(self) -> None:
        """Verify tickPrice callbacks from other request IDs are ignored."""
        client = mock.Mock(spec=TWSClient)
        client.is_connected.return_value = True

        settings = Settings()
        adapter = IBKRMarketDataAdapter(client, settings)
        adapter.request_market_data()

        # Wrong reqId callback
        adapter.on_tick_price(9999, 4, 150.25)
        assert adapter.queue_size() == 0

    def test_tick_price_ignores_non_trade_tick_types(self) -> None:
        """Verify that bid/ask prices are not parsed as candle trade prices."""
        client = mock.Mock(spec=TWSClient)
        client.is_connected.return_value = True

        settings = Settings()
        adapter = IBKRMarketDataAdapter(client, settings)
        req_id = adapter.request_market_data()

        # Bid (1) and Ask (2) should be filtered
        adapter.on_tick_price(req_id, 1, 150.25)
        adapter.on_tick_price(req_id, 2, 150.25)
        assert adapter.queue_size() == 0

    def test_tick_price_ignores_invalid_values(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verify zero or negative prices are logged and skipped."""
        client = mock.Mock(spec=TWSClient)
        client.is_connected.return_value = True

        settings = Settings()
        adapter = IBKRMarketDataAdapter(client, settings)
        req_id = adapter.request_market_data()

        with caplog.at_level(logging.WARNING):
            adapter.on_tick_price(req_id, 4, 0.0)
            adapter.on_tick_price(req_id, 4, -10.5)

        assert adapter.queue_size() == 0
        assert "Ignored non-positive price callback" in caplog.text

    def test_tick_size_ignores_negative_size(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verify negative sizes are logged and ignored."""
        client = mock.Mock(spec=TWSClient)
        client.is_connected.return_value = True

        settings = Settings()
        adapter = IBKRMarketDataAdapter(client, settings)
        req_id = adapter.request_market_data()

        with caplog.at_level(logging.WARNING):
            adapter.on_tick_size(req_id, 5, -100)

        assert adapter.queue_size() == 0
        assert "Ignored negative size callback" in caplog.text

    def test_market_data_type_handling(self, caplog: pytest.LogCaptureFixture) -> None:
        """Verify marketDataType callbacks are received and logged."""
        client = mock.Mock(spec=TWSClient)
        client.is_connected.return_value = True

        settings = Settings()
        adapter = IBKRMarketDataAdapter(client, settings)
        req_id = adapter.request_market_data()

        with caplog.at_level(logging.INFO):
            adapter.on_market_data_type(req_id, 3)

        assert "TWS confirmed market data type" in caplog.text

    def test_cancellation(self) -> None:
        """Verify cancel_market_data halts active request and notifies TWS."""
        client = mock.Mock(spec=TWSClient)
        client.is_connected.return_value = True

        settings = Settings()
        adapter = IBKRMarketDataAdapter(client, settings)
        req_id = adapter.request_market_data()

        adapter.cancel_market_data()

        assert adapter._active_req_id is None
        assert adapter._active_symbol is None
        client.cancelMktData.assert_called_once_with(req_id)

    def test_no_events_after_cancellation(self) -> None:
        """Verify no events are enqueued from callbacks post-cancellation."""
        client = mock.Mock(spec=TWSClient)
        client.is_connected.return_value = True

        settings = Settings()
        adapter = IBKRMarketDataAdapter(client, settings)
        adapter.request_market_data()

        adapter.cancel_market_data()
        assert adapter.queue_size() == 0

        # Fire callbacks after cancellation
        adapter.on_tick_price(1000, 4, 150.00)
        assert adapter.queue_size() == 0

    def test_no_credential_leakage(self, caplog: pytest.LogCaptureFixture) -> None:
        """Verify logger outputs symbol metadata but does not leak credentials."""
        client = mock.Mock(spec=TWSClient)
        client.is_connected.return_value = True

        settings = Settings()
        adapter = IBKRMarketDataAdapter(client, settings)

        with caplog.at_level(logging.INFO):
            adapter.request_market_data()

        assert settings.ibkr_market_data_symbol in caplog.text
        assert "password" not in caplog.text
        assert "secret" not in caplog.text

    # ── Hardened Event Semantics & Callback Sequencing Tests ────────

    def test_event_semantics_price_before_size(self) -> None:
        """Sequence A: price callback followed by size callback.

        Should emit exactly one combined MarketDataEvent.
        """
        client = mock.Mock(spec=TWSClient)
        client.is_connected.return_value = True
        adapter = IBKRMarketDataAdapter(client, Settings())
        req_id = adapter.request_market_data()

        # Step 1: price callback
        adapter.on_tick_price(req_id, 4, 100.0)
        assert adapter.queue_size() == 0  # Not emitted yet, waiting for size

        # Step 2: size callback
        adapter.on_tick_size(req_id, 5, 50)
        assert adapter.queue_size() == 1  # Emitted exactly one event

        event = adapter.get_event()
        assert event is not None
        assert event.price == Decimal("100.0")
        assert event.volume == 50

    def test_event_semantics_size_before_price(self) -> None:
        """Sequence B: size callback followed by price callback.

        Should emit exactly one combined MarketDataEvent.
        """
        client = mock.Mock(spec=TWSClient)
        client.is_connected.return_value = True
        adapter = IBKRMarketDataAdapter(client, Settings())
        req_id = adapter.request_market_data()

        # Step 1: size callback
        adapter.on_tick_size(req_id, 5, 50)
        assert adapter.queue_size() == 0  # Not emitted yet, waiting for price

        # Step 2: price callback
        adapter.on_tick_price(req_id, 4, 100.0)
        assert adapter.queue_size() == 1  # Emitted exactly one event

        event = adapter.get_event()
        assert event is not None
        assert event.price == Decimal("100.0")
        assert event.volume == 50

    def test_event_semantics_repeated_prices(self) -> None:
        """Sequence C: tickPrice(100), tickPrice(101), tickSize(50).

        Should emit price 100 (flushed with volume 0) and price 101 with volume 50.
        """
        client = mock.Mock(spec=TWSClient)
        client.is_connected.return_value = True
        adapter = IBKRMarketDataAdapter(client, Settings())
        req_id = adapter.request_market_data()

        adapter.on_tick_price(req_id, 4, 100.0)
        assert adapter.queue_size() == 0

        # Changing price flushes the pending price update
        adapter.on_tick_price(req_id, 4, 101.0)
        assert adapter.queue_size() == 1

        event1 = adapter.get_event()
        assert event1 is not None
        assert event1.price == Decimal("100.0")
        assert event1.volume == 0

        # Now size 50 completes the second price update
        adapter.on_tick_size(req_id, 5, 50)
        assert adapter.queue_size() == 1

        event2 = adapter.get_event()
        assert event2 is not None
        assert event2.price == Decimal("101.0")
        assert event2.volume == 50

    def test_event_semantics_repeated_sizes(self) -> None:
        """Sequence E: tickSize(50), tickSize(30), tickPrice(101).

        Should emit exactly one combined event (101, 30), overwriting the old size.
        """
        client = mock.Mock(spec=TWSClient)
        client.is_connected.return_value = True
        adapter = IBKRMarketDataAdapter(client, Settings())
        req_id = adapter.request_market_data()

        adapter.on_tick_size(req_id, 5, 50)
        adapter.on_tick_size(req_id, 5, 30)
        assert adapter.queue_size() == 0

        adapter.on_tick_price(req_id, 4, 101.0)
        assert adapter.queue_size() == 1

        event = adapter.get_event()
        assert event is not None
        assert event.price == Decimal("101.0")
        assert event.volume == 30

    def test_event_semantics_stale_volume_prevention(self) -> None:
        """Sequence D: tickPrice(100) -> tickSize(50) -> tickPrice(101) -> tickSize(30).

        Verify no stale volume is carried over or reused.
        """
        client = mock.Mock(spec=TWSClient)
        client.is_connected.return_value = True
        adapter = IBKRMarketDataAdapter(client, Settings())
        req_id = adapter.request_market_data()

        adapter.on_tick_price(req_id, 4, 100.0)
        adapter.on_tick_size(req_id, 5, 50)

        # Consume the first trade event
        assert adapter.queue_size() == 1
        event1 = adapter.get_event()
        assert event1 is not None
        assert event1.price == Decimal("100.0")
        assert event1.volume == 50

        # Trigger second price update
        adapter.on_tick_price(req_id, 4, 101.0)
        assert adapter.queue_size() == 0  # Stale volume 50 was NOT reused!

        adapter.on_tick_size(req_id, 5, 30)
        assert adapter.queue_size() == 1
        event2 = adapter.get_event()
        assert event2 is not None
        assert event2.price == Decimal("101.0")
        assert event2.volume == 30

    # ── Connection Lifecycle Tests ──────────────────────────────────

    def test_lifecycle_connection_closed(self) -> None:
        """Verify connectionClosed callback triggers disconnect handling.

        Should flush pending price events and clear subscription states.
        """
        client = mock.Mock(spec=TWSClient)
        client.is_connected.return_value = True
        adapter = IBKRMarketDataAdapter(client, Settings())
        req_id = adapter.request_market_data()

        # Cache a pending price update
        adapter.on_tick_price(req_id, 4, 150.00)
        assert adapter.queue_size() == 0

        # Connection drops
        adapter.on_connection_closed()

        # Check pending price was flushed
        assert adapter.queue_size() == 1
        event = adapter.get_event()
        assert event is not None
        assert event.price == Decimal("150.00")
        assert event.volume == 0

        # Verify active states are fully cleared
        assert adapter._active_req_id is None
        assert adapter._active_symbol is None

    def test_lifecycle_reconnect_resubscription(self) -> None:
        """Verify resubscription is allowed after disconnect and reconnect."""
        client = mock.Mock(spec=TWSClient)
        client.is_connected.return_value = True
        adapter = IBKRMarketDataAdapter(client, Settings())

        # First connection/subscription
        req_id1 = adapter.request_market_data()
        assert isinstance(req_id1, int)

        # Disconnection
        adapter.on_connection_closed()
        assert adapter._active_req_id is None

        # Re-connection and secondary subscription
        req_id2 = adapter.request_market_data()
        assert req_id2 == req_id1 + 1  # Newly generated request ID

    def test_lifecycle_cancel_market_data_flushes_pending(self) -> None:
        """Verify cancel_market_data flushes any pending un-paired price update."""
        client = mock.Mock(spec=TWSClient)
        client.is_connected.return_value = True
        adapter = IBKRMarketDataAdapter(client, Settings())
        req_id = adapter.request_market_data()

        adapter.on_tick_price(req_id, 4, 150.0)
        assert adapter.queue_size() == 0

        adapter.cancel_market_data()
        assert adapter.queue_size() == 1

        event = adapter.get_event()
        assert event is not None
        assert event.price == Decimal("150.0")
        assert event.volume == 0

    # ── Concurrency & Thread-Safety Tests ───────────────────────────

    def test_concurrency_callback_while_cancelling(self) -> None:
        """Verify that callbacks arriving during cancellation do not leak events."""
        client = mock.Mock(spec=TWSClient)
        client.is_connected.return_value = True
        adapter = IBKRMarketDataAdapter(client, Settings())
        adapter.request_market_data()

        # Simulate cancellation executing on thread A, while a callback executes on thread B
        # Let's acquire lock, perform cancel, and then try calling callback.
        adapter.cancel_market_data()

        # Callback executed after cancellation
        adapter.on_tick_price(1000, 4, 100.0)
        adapter.on_tick_size(1000, 5, 20)

        # Queue should be empty (no events processed after cancel completes)
        assert adapter.queue_size() == 0

    def test_concurrency_listener_iteration_safe(self) -> None:
        """Verify dispatching EWrapper callbacks iterates over list copies.

        Ensures list iteration is safe from concurrent list mutations.
        """
        client = TWSClient()

        # Simulate listener that registers another listener during callback execution
        class DynamicListener:
            def on_tick_price(self, reqId: int, tickType: int, price: float) -> None:
                # Mutate listeners list inside callback
                client.register_market_data_listener(DynamicListener())

        client.register_market_data_listener(DynamicListener())

        # Dispatch tickPrice: should not raise RuntimeError: list size changed during iteration
        try:
            client.tickPrice(1, 4, 150.0, None)
        except RuntimeError as e:
            pytest.fail(f"TWSClient callback dispatch was not thread-safe: {e}")
