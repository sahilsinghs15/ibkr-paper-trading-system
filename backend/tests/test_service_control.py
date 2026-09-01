"""Unit tests for the Service Control API route."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.deps import require_admin
from app.db.models.user import UserModel
from app.main import app


@pytest.fixture
def mock_admin():
    admin_user = UserModel(
        id=1,
        email="admin@example.com",
        password_hash="hashed_pw",
        role="admin",
        is_active=True,
    )
    app.dependency_overrides[require_admin] = lambda: admin_user
    yield admin_user
    app.dependency_overrides.pop(require_admin, None)



@pytest.fixture
def client():
    with (
        patch("app.broker.ibkr.tws_client.TWSClient.connect_and_start", return_value=True),
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
            "app.services.recovery.RecoveryManager.run_startup_recovery",
            new_callable=AsyncMock,
        ),
        patch("app.services.order_manager.OrderManager.hydrate_live_pnl", new_callable=AsyncMock),
        TestClient(app) as c,
    ):
        yield c



def test_service_control_list_allowed(client: TestClient, mock_admin):
    """Verify GET /api/v1/service-control/allowed returns allowlisted services and actions."""
    res = client.get("/api/v1/service-control/allowed")
    assert res.status_code == 200, res.text
    data = res.json()
    assert "ibgateway" in data["services"]
    assert "backend" in data["services"]
    assert "webhook" in data["services"]
    assert "watchdog" in data["services"]
    assert "process_manager" not in data["services"]
    assert "start" in data["actions"]
    assert "stop" in data["actions"]


def test_service_control_unauthorized(client: TestClient):
    """Verify non-admin user request is rejected with 403 Forbidden."""
    def raise_forbidden():
        raise HTTPException(status_code=403, detail="Operation restricted to administrator role")

    app.dependency_overrides[require_admin] = raise_forbidden
    try:
        res = client.post("/api/v1/service-control/ibgateway/start")
        assert res.status_code == 403
    finally:
        app.dependency_overrides.pop(require_admin, None)




def test_service_control_disallowed_service(client: TestClient, mock_admin):
    """Verify requesting an unallowlisted service (e.g. process_manager or arbitrary string) is rejected."""
    res = client.post("/api/v1/service-control/process_manager/start")
    assert res.status_code in (400, 422)

    res2 = client.post("/api/v1/service-control/nginx/start")
    assert res2.status_code in (400, 422)


def test_service_control_disallowed_action(client: TestClient, mock_admin):
    """Verify requesting an unallowlisted action (e.g. kill or exec) is rejected."""
    res = client.post("/api/v1/service-control/ibgateway/kill")
    assert res.status_code in (400, 422)


@pytest.mark.parametrize(
    "service_key,expected_unit",
    [
        ("ibgateway", "ibgateway.service"),
        ("backend", "trading-backend.service"),
        ("webhook", "webhook-ingest.service"),
        ("watchdog", "watchdog.service"),
    ],
)
def test_service_control_start_allowed_services(
    client: TestClient, mock_admin, service_key: str, expected_unit: str
):
    """Verify starting each allowlisted service invokes systemctl safely without shell."""
    mock_run = MagicMock()
    mock_run.returncode = 0
    mock_run.stdout = "Job for unit succeeded"
    mock_run.stderr = ""

    with patch("subprocess.run", return_value=mock_run) as patched_subprocess:
        res = client.post(f"/api/v1/service-control/{service_key}/start")
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["result"] == "ok"
        assert data["service"] == service_key
        assert data["unit"] == expected_unit

        # Verify subprocess.run was called with fixed list [systemctl, start, expected_unit]
        patched_subprocess.assert_called_once_with(
            ["systemctl", "start", expected_unit],
            capture_output=True,
            text=True,
            timeout=15,
        )


def test_service_control_status_action(client: TestClient, mock_admin):
    """Verify status action queries systemctl is-active and systemctl show."""
    mock_run_active = MagicMock()
    mock_run_active.stdout = "active\n"

    mock_run_show = MagicMock()
    mock_run_show.stdout = "ActiveState=active\nSubState=running\nMainPID=12345\n"

    def side_effect(cmd, **kwargs):
        if "is-active" in cmd:
            return mock_run_active
        return mock_run_show

    with patch("subprocess.run", side_effect=side_effect):
        res = client.post("/api/v1/service-control/webhook/status")
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["service"] == "webhook"
        assert data["unit"] == "webhook-ingest.service"
        assert data["active"] == "active"
        assert "MainPID=12345" in data["details"]


@pytest.mark.parametrize(
    "service_key,expected_unit",
    [
        ("ibgateway", "ibgateway.service"),
        ("backend", "trading-backend.service"),
        ("webhook", "webhook-ingest.service"),
        ("watchdog", "watchdog.service"),
    ],
)
def test_service_control_restart_allowed_services(
    client: TestClient, mock_admin, service_key: str, expected_unit: str
):
    """Verify restarting each allowlisted service invokes systemctl restart safely without shell."""
    mock_run = MagicMock()
    mock_run.returncode = 0
    mock_run.stdout = "Job for unit succeeded"
    mock_run.stderr = ""

    with patch("subprocess.run", return_value=mock_run) as patched_subprocess:
        res = client.post(f"/api/v1/service-control/{service_key}/restart")
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["result"] == "ok"
        assert data["service"] == service_key
        assert data["unit"] == expected_unit

        patched_subprocess.assert_called_once_with(
            ["systemctl", "restart", expected_unit],
            capture_output=True,
            text=True,
            timeout=15,
        )

