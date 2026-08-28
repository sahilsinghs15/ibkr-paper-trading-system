"""Unit tests for TWSClient connection lifecycle and configuration."""

import logging
from unittest import mock

import pytest

from app.broker.ibkr.tws_client import TWSClient
from app.core.config import Settings


class TestTWSConnection:
    def test_default_configuration(self) -> None:
        """Verify settings default connection values."""
        settings = Settings(_env_file=None)
        assert settings.ibkr_host == "127.0.0.1"
        assert settings.ibkr_port == 7497
        assert settings.ibkr_client_id == 1
        assert settings.ibkr_connection_timeout == 10

    def test_custom_configuration(self) -> None:
        """Verify settings custom overrides."""
        settings = Settings(
            ibkr_host="192.168.1.100",
            ibkr_port=4002,
            ibkr_client_id=99,
            ibkr_connection_timeout=5,
        )
        assert settings.ibkr_host == "192.168.1.100"
        assert settings.ibkr_port == 4002
        assert settings.ibkr_client_id == 99
        assert settings.ibkr_connection_timeout == 5

    def test_initial_disconnected_state(self) -> None:
        """Verify client starts in disconnected state with empty order ID."""
        client = TWSClient()
        assert client.is_connected() is False
        assert client.next_order_id is None

    def test_successful_connection_and_handshake(self) -> None:
        """Verify successful connection and handshake state transitions."""
        client = TWSClient()

        # Mock socket connect and message processing run loop
        with (
            mock.patch.object(client, "connect") as mock_connect,
            mock.patch.object(client, "run"),
        ):
            # Override wait to simulate nextValidId callback execution
            def dummy_wait(*args, **kwargs) -> bool:
                client.nextValidId(100)
                return True

            with (
                mock.patch.object(
                    client._connected_event, "wait", side_effect=dummy_wait
                ),
                mock.patch.object(client, "isConnected", return_value=True),
            ):
                success = client.connect_and_start("localhost", 7497, 1)
                assert success is True
                assert client.is_connected() is True
                assert client.next_order_id == 100
                mock_connect.assert_called_once_with("localhost", 7497, 1)

    def test_connection_handshake_timeout(self) -> None:
        """Verify connection failure on handshake timeout."""
        client = TWSClient()
        with (
            mock.patch.object(client, "connect"),
            mock.patch.object(client, "run"),
            mock.patch.object(client, "disconnect") as mock_disconnect,
            mock.patch.object(client._connected_event, "wait", return_value=False),
        ):
            success = client.connect_and_start("localhost", 7497, 1, timeout=0.1)
            assert success is False
            assert client.is_connected() is False
            assert client.next_order_id is None
            mock_disconnect.assert_called_once()

    def test_connection_failure_behavior(self) -> None:
        """Verify connection error propagation handles socket exceptions."""
        client = TWSClient()
        with mock.patch.object(
            client,
            "connect",
            side_effect=ConnectionRefusedError("Connection refused"),
        ):
            success = client.connect_and_start("localhost", 7497, 1)
            assert success is False
            assert client.is_connected() is False
            assert client.next_order_id is None

    def test_disconnect_state_transition(self) -> None:
        """Verify disconnect clears state variables cleanly."""
        client = TWSClient()
        client._connected_event.set()
        client.next_order_id = 50

        with mock.patch.object(client, "disconnect") as mock_disconnect:
            client.disconnect_clean()
            assert client.is_connected() is False
            assert client.next_order_id is None
            mock_disconnect.assert_called_once()

    def test_error_callback_handling(self, caplog: pytest.LogCaptureFixture) -> None:
        """Verify error notifications are handled and logged by category."""
        client = TWSClient()

        # Status info codes (2000-2999)
        with caplog.at_level(logging.INFO):
            client.error(1, 2104, "Market data farm connection is OK")
            assert "TWS Status Notification" in caplog.text
            assert "2104" in caplog.text

        caplog.clear()

        # Actual API errors
        with caplog.at_level(logging.WARNING):
            client.error(1, 502, "Couldn't connect to TWS")
            assert "TWS API Error" in caplog.text
            assert "502" in caplog.text

    def test_no_credential_leakage(self, caplog: pytest.LogCaptureFixture) -> None:
        """Verify that connection log messages do not expose secrets."""
        client = TWSClient()
        with (
            mock.patch.object(client, "connect"),
            mock.patch.object(client, "run"),
            caplog.at_level(logging.INFO),
        ):
            client.connect_and_start("somehost.com", 7497, 1)
            assert "somehost.com" in caplog.text
            assert "7497" in caplog.text
            # Confirm basic attributes only
            assert "password" not in caplog.text
            assert "secret" not in caplog.text
