import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# import helper as module (filename has hyphen, register as notify_telegram)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import importlib.util

spec = importlib.util.spec_from_file_location("notify_telegram", Path(__file__).resolve().parents[2] / "scripts" / "notify-telegram.py")
notify = importlib.util.module_from_spec(spec)  # pyrefly: ignore[bad-argument-type]
sys.modules["notify_telegram"] = notify
spec.loader.exec_module(notify)  # pyrefly: ignore[missing-attribute]


def _mock_settings(enabled=True, token="tok", chat="123"):
    m = MagicMock()
    m.telegram_enabled = enabled
    m.telegram_bot_token = token
    m.telegram_chat_id = chat
    m.telegram_timeout_seconds = 5.0
    m.telegram_max_retries = 1
    m.telegram_rate_limit_per_sec = 1.0
    return m


@patch("notify_telegram._load_settings")
@patch("app.services.watchdog.telegram.TelegramClient")
def test_start_message(mock_client_cls, mock_settings):
    mock_settings.return_value = _mock_settings()
    mock_client = MagicMock()
    mock_client.send_message = AsyncMock(return_value=True)
    mock_client_cls.return_value = mock_client
    rc = notify.main(["prog", "start", "ibgateway"])
    assert rc == 0
    # check message sent contains service + started and correct icon
    args, _kwargs = mock_client.send_message.call_args
    text = args[0]
    assert "ibgateway" in text
    assert "started" in text
    assert "\U0001f7e2" in text
    assert "PID" not in text


@patch("notify_telegram._load_settings")
@patch("app.services.watchdog.telegram.TelegramClient")
def test_stop_message(mock_client_cls, mock_settings):
    mock_settings.return_value = _mock_settings()
    mock_client = MagicMock()
    mock_client.send_message = AsyncMock(return_value=True)
    mock_client_cls.return_value = mock_client
    rc = notify.main(["prog", "stop", "trading-backend"])
    assert rc == 0
    text = mock_client.send_message.call_args[0][0]
    assert "trading-backend" in text
    assert "stopped" in text
    assert "\U0001f534" in text


@patch("notify_telegram._load_settings")
def test_disabled_no_send(mock_settings):
    mock_settings.return_value = _mock_settings(enabled=False)
    with patch("app.services.watchdog.telegram.TelegramClient") as mock_cls:
        rc = notify.main(["prog", "start", "demo-streaming"])
        assert rc == 0
        mock_cls.assert_not_called()


@patch("notify_telegram._load_settings")
def test_missing_config_no_failure(mock_settings):
    mock_settings.return_value = _mock_settings(token=None, chat=None)
    # should exit 0, no exception, no send
    rc = notify.main(["prog", "stop", "webhook-ingest"])
    assert rc == 0


@patch("notify_telegram._load_settings")
@patch("app.services.watchdog.telegram.TelegramClient")
def test_api_failure_exits_safely(mock_client_cls, mock_settings):
    mock_settings.return_value = _mock_settings()
    mock_client = MagicMock()
    mock_client.send_message = AsyncMock(side_effect=Exception("network"))
    mock_client_cls.return_value = mock_client
    rc = notify.main(["prog", "start", "ibgateway"])
    assert rc == 0  # must not propagate


def test_start_message_format_concise():
    # ensure helper does not add verbose fields
    with patch("notify_telegram._load_settings", return_value=_mock_settings()), patch("app.services.watchdog.telegram.TelegramClient") as mock_cls:
        mock_client = MagicMock()
        mock_client.send_message = AsyncMock(return_value=True)
        mock_cls.return_value = mock_client
        notify.main(["prog", "start", "demo-streaming"])
        text = mock_client.send_message.call_args[0][0]
        assert text == "\U0001f7e2 demo-streaming started"
        assert len(text.split()) == 3  # icon + service + verb


def test_stop_message_format_concise():
    with patch("notify_telegram._load_settings", return_value=_mock_settings()), patch("app.services.watchdog.telegram.TelegramClient") as mock_cls:
        mock_client = MagicMock()
        mock_client.send_message = AsyncMock(return_value=True)
        mock_cls.return_value = mock_client
        notify.main(["prog", "stop", "webhook-ingest"])
        text = mock_client.send_message.call_args[0][0]
        assert text == "\U0001f534 webhook-ingest stopped"


def test_market_closed_once_per_day(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    monkeypatch.setenv("NOTIFY_STATE_FILE", str(state))
    # reload module state file path? helper reads env at import, but we set STATE_FILE dynamically
    notify.STATE_FILE = state  # pyrefly: ignore[missing-attribute]
    with patch("app.services.session_clock.is_trading_day", return_value=False), \
         patch("notify_telegram._market_closed_reason", return_value="Saturday"), \
         patch("notify_telegram._load_settings", return_value=_mock_settings()), \
         patch("app.services.watchdog.telegram.TelegramClient") as mock_cls:
        mock_client = MagicMock()
        mock_client.send_message = AsyncMock(return_value=True)
        mock_cls.return_value = mock_client
        rc1 = notify.main(["prog", "market-closed"])
        rc2 = notify.main(["prog", "market-closed"])
        assert rc1 == 0 and rc2 == 0
        # only first sends
        assert mock_client.send_message.call_count == 1
        assert state.exists()
        data = json.loads(state.read_text())
        assert "date" in data


def test_saturday_market_closed(tmp_path, monkeypatch):
    state = tmp_path / "state2.json"
    monkeypatch.setenv("NOTIFY_STATE_FILE", str(state))
    notify.STATE_FILE = state  # pyrefly: ignore[missing-attribute]
    # force today to be Saturday via is_trading_day False + reason Saturday
    with patch("app.services.session_clock.is_trading_day", return_value=False), \
         patch("notify_telegram._market_closed_reason", return_value="Saturday"), \
         patch("notify_telegram._load_settings", return_value=_mock_settings()), \
         patch("app.services.watchdog.telegram.TelegramClient") as mock_cls:
        mock_client = MagicMock()
        mock_client.send_message = AsyncMock(return_value=True)
        mock_cls.return_value = mock_client
        notify.main(["prog", "market-closed"])
        text = mock_client.send_message.call_args[0][0]
        assert "Saturday" in text
        assert "\U0001f4c5" in text


def test_sunday_market_closed(tmp_path, monkeypatch):
    state = tmp_path / "state3.json"
    notify.STATE_FILE = state  # pyrefly: ignore[missing-attribute]
    with patch("app.services.session_clock.is_trading_day", return_value=False), \
         patch("notify_telegram._market_closed_reason", return_value="Sunday"), \
         patch("notify_telegram._load_settings", return_value=_mock_settings()), \
         patch("app.services.watchdog.telegram.TelegramClient") as mock_cls:
        mock_client = MagicMock()
        mock_client.send_message = AsyncMock(return_value=True)
        mock_cls.return_value = mock_client
        notify.main(["prog", "market-closed"])
        assert "Sunday" in mock_client.send_message.call_args[0][0]


def test_holiday_market_closed(tmp_path, monkeypatch):
    state = tmp_path / "state4.json"
    notify.STATE_FILE = state  # pyrefly: ignore[missing-attribute]
    with patch("app.services.session_clock.is_trading_day", return_value=False), \
         patch("notify_telegram._market_closed_reason", return_value="Labor Day"), \
         patch("notify_telegram._load_settings", return_value=_mock_settings()), \
         patch("app.services.watchdog.telegram.TelegramClient") as mock_cls:
        mock_client = MagicMock()
        mock_client.send_message = AsyncMock(return_value=True)
        mock_cls.return_value = mock_client
        notify.main(["prog", "market-closed"])
        assert "Labor Day" in mock_client.send_message.call_args[0][0]


def test_trading_day_no_market_closed(tmp_path, monkeypatch):
    state = tmp_path / "state5.json"
    notify.STATE_FILE = state  # pyrefly: ignore[missing-attribute]
    with patch("app.services.session_clock.is_trading_day", return_value=True), \
         patch("notify_telegram._load_settings", return_value=_mock_settings()), \
         patch("app.services.watchdog.telegram.TelegramClient") as mock_cls:
        mock_client = MagicMock()
        mock_client.send_message = AsyncMock(return_value=True)
        mock_cls.return_value = mock_client
        notify.main(["prog", "market-closed"])
        mock_client.send_message.assert_not_called()
        # no state file created
        assert not state.exists()


def test_early_close_no_market_closed(tmp_path, monkeypatch):
    # Early-close day is_trading_day True → no message
    state = tmp_path / "state6.json"
    notify.STATE_FILE = state  # pyrefly: ignore[missing-attribute]
    with patch("app.services.session_clock.is_trading_day", return_value=True), \
         patch("notify_telegram._load_settings", return_value=_mock_settings()), \
         patch("app.services.watchdog.telegram.TelegramClient") as mock_cls:
        mock_client = MagicMock()
        mock_client.send_message = AsyncMock(return_value=True)
        mock_cls.return_value = mock_client
        notify.main(["prog", "market-closed"])
        mock_client.send_message.assert_not_called()


def test_helper_no_systemctl():
    content = (Path(__file__).resolve().parents[2] / "scripts" / "notify-telegram.py").read_text()
    # ensure no actual systemctl call (ignore comment mentioning it)
    assert "systemctl start" not in content
    assert "systemctl stop" not in content
    assert "systemctl restart" not in content
