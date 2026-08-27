"""Unit and integration tests for the System Monitor API."""

from unittest.mock import AsyncMock, patch
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.system_monitor import SystemMonitorResponse
from app.services.system_monitor_service import collect_system_monitor_data


@pytest.fixture
def client():
    with (
        patch(
            "app.broker.ibkr.tws_client.TWSClient.connect_and_start",
            return_value=True,
        ),
        patch("app.broker.ibkr.tws_client.TWSClient.disconnect_clean"),
        patch("app.broker.ibkr.tws_client.TWSClient.is_connected", return_value=False),
        patch("app.services.worker_pool.ExecutionWorkerPool.start", new_callable=AsyncMock),
        patch("app.services.worker_pool.ExecutionWorkerPool.stop", new_callable=AsyncMock),
        patch(
            "app.services.position_reconciler.PositionReconciler.start",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.position_reconciler.PositionReconciler.stop",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.order_manager.OrderManager.hydrate_live_pnl",
            new_callable=AsyncMock,
        ),
        TestClient(app) as c,
    ):
        yield c


def test_system_monitor_endpoint_structure(client: TestClient):
    """Verify GET /api/v1/system-monitor returns HTTP 200 with valid schema."""
    response = client.get("/api/v1/system-monitor")
    assert response.status_code == 200, response.text
    data = response.json()

    # Validate against Pydantic schema
    validated = SystemMonitorResponse.model_validate(data)
    assert validated.overall_status in ("HEALTHY", "DEGRADED", "CRITICAL")
    assert validated.system.hostname != ""
    assert validated.cpu.count >= 1
    assert validated.memory.ram.total_bytes > 0
    assert len(validated.storage) >= 1
    assert validated.storage[0].mount == "/"

    # Services presence
    assert validated.services.backend.name == "FastAPI Backend"
    assert validated.services.demo_stream.name == "Demo Streaming"
    assert validated.services.ib_gateway.name == "IB Gateway"
    assert validated.services.postgresql.name == "PostgreSQL"
    assert validated.services.redis.name == "Redis"


def test_system_monitor_secrets_not_exposed(client: TestClient):
    """Verify secrets/passwords/credentials are never returned in system monitor response."""
    response = client.get("/api/v1/system-monitor")
    assert response.status_code == 200
    raw_text = response.text.lower()

    # Common secret keywords
    forbidden_terms = ["password", "secret_key", "webhook_auth_secret", "postgres:root"]
    for term in forbidden_terms:
        assert term not in raw_text, f"Forbidden secret term '{term}' exposed in response!"


@pytest.mark.asyncio
async def test_partial_service_failure_does_not_crash():
    """Verify that if external service checks fail/timeout, data is returned with DEGRADED/CRITICAL status."""
    with patch("httpx.AsyncClient.get", side_effect=Exception("Connection refused")):
        res = await collect_system_monitor_data(session=None, tws_client=None, redis_client=None)
        assert res.services.demo_stream.status == "STOPPED"
        assert "Unreachable" in res.services.demo_stream.health_detail
        assert res.overall_status in ("DEGRADED", "CRITICAL")
        # Ensure non-crashing valid object
        assert res.system.hostname != ""


@pytest.mark.asyncio
async def test_ib_gateway_dynamic_port_configuration():
    """Verify IB Gateway probe dynamically uses get_settings().ibkr_port and host."""
    from app.core.config import Settings, get_settings

    # Mock settings with port 4002 (MAIN EC2 posture)
    mock_settings_4002 = Settings(ibkr_host="127.0.0.1", ibkr_port=4002)

    with patch("app.services.system_monitor_service.get_settings", return_value=mock_settings_4002):
        with patch("asyncio.open_connection", new_callable=AsyncMock) as mock_open:
            mock_reader = AsyncMock()
            mock_writer = AsyncMock()
            mock_open.return_value = (mock_reader, mock_writer)

            res = await collect_system_monitor_data(session=None, tws_client=None, redis_client=None)

            # Check open_connection was called with 127.0.0.1:4002
            mock_open.assert_any_call("127.0.0.1", 4002)
            assert res.services.ib_gateway.port == 4002
            assert res.services.ib_gateway.status == "RUNNING"
            assert 4002 in res.network["open_ports"]


@pytest.mark.asyncio
async def test_ib_gateway_dynamic_port_7497():
    """Verify IB Gateway probe dynamically uses port 7497 when configured."""
    from app.core.config import Settings

    mock_settings_7497 = Settings(ibkr_host="127.0.0.1", ibkr_port=7497)

    with patch("app.services.system_monitor_service.get_settings", return_value=mock_settings_7497):
        with patch("asyncio.open_connection", new_callable=AsyncMock) as mock_open:
            mock_reader = AsyncMock()
            mock_writer = AsyncMock()
            mock_open.return_value = (mock_reader, mock_writer)

            res = await collect_system_monitor_data(session=None, tws_client=None, redis_client=None)

            mock_open.assert_any_call("127.0.0.1", 7497)
            assert res.services.ib_gateway.port == 7497
            assert res.services.ib_gateway.status == "RUNNING"
            assert 7497 in res.network["open_ports"]

