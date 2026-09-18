"""Client-origin integrity across the dashboard proxy, and session revocation there."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from demo_streaming.api import create_demo_app
from tests.audit_test_utils import client_for, create_user, install_app_state, login


def _demo_app(session_factory):
    redis_mock = MagicMock()
    redis_mock.ping = AsyncMock(return_value=True)
    redis_mock.xread = AsyncMock(return_value=[])
    return create_demo_app(
        session_factory=session_factory, redis=redis_mock, stream_name="positions:stream"
    )


async def test_dashboard_proxy_replaces_client_supplied_forwarding_headers(session_factory):
    captured: dict[str, dict[str, str]] = {}

    class FakeUpstream:
        """Stands in for the proxy's upstream httpx client only."""

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, method, url, content=None, headers=None, **kwargs):
            captured["headers"] = {k.lower(): v for k, v in (headers or {}).items()}
            return httpx.Response(
                200, json={"ok": True}, headers={"content-type": "application/json"}
            )

    app = _demo_app(session_factory)
    with patch("demo_streaming.api.httpx.AsyncClient", FakeUpstream):
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("198.51.100.200", 40000)),
            base_url="http://dashboard",
        ) as client:
            res = await client.get(
                "/api/v1/auth/me",
                headers={
                    "Authorization": "Bearer abc",
                    "X-Forwarded-For": "6.6.6.6, 7.7.7.7",
                    "X-Real-IP": "8.8.8.8",
                    "Forwarded": "for=9.9.9.9",
                    "X-Client-Device-Id": "dev-12345678-abcd",
                },
            )
    assert res.status_code == 200
    headers = captured["headers"]
    assert headers["x-forwarded-for"] == "198.51.100.200"
    assert "x-real-ip" not in headers
    assert "forwarded" not in headers
    # Authorization and client-reported (non-authoritative) context still pass through.
    assert headers["authorization"] == "Bearer abc"
    assert headers["x-client-device-id"] == "dev-12345678-abcd"


async def test_dashboard_rejects_tokens_of_ended_sessions(session_factory):
    install_app_state(session_factory)
    user = await create_user(session_factory)
    async with client_for() as api:
        token = await login(api, user)
    demo = _demo_app(session_factory)
    async with AsyncClient(transport=ASGITransport(app=demo), base_url="http://dashboard") as c:
        before = await c.get("/demo/event-journal", headers={"Authorization": f"Bearer {token}"})
    assert before.status_code == 200

    async with client_for() as api:
        out = await api.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"})
        assert out.status_code == 200

    async with AsyncClient(transport=ASGITransport(app=demo), base_url="http://dashboard") as c:
        after = await c.get("/demo/event-journal", headers={"Authorization": f"Bearer {token}"})
    assert after.status_code == 401


@pytest.mark.parametrize("path", ["/demo/audit-logs"])
async def test_legacy_audit_logs_endpoint_is_retired(session_factory, path):
    demo = _demo_app(session_factory)
    async with AsyncClient(transport=ASGITransport(app=demo), base_url="http://dashboard") as c:
        res = await c.get(path)
    assert res.status_code == 404


async def test_trading_api_trusts_forwarded_ip_only_from_loopback_proxy(session_factory):
    """Mirror production: uvicorn's ProxyHeadersMiddleware (trusts 127.0.0.1 only)."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from app.main import app as trading_app
    from tests.audit_test_utils import TEST_PASSWORD, single_audit

    install_app_state(session_factory)
    wrapped = ProxyHeadersMiddleware(trading_app, trusted_hosts="127.0.0.1")
    via_dashboard = await create_user(session_factory)
    direct = await create_user(session_factory)

    async with AsyncClient(
        transport=ASGITransport(app=wrapped, client=("127.0.0.1", 5000)), base_url="http://api"
    ) as c:
        res = await c.post(
            "/api/v1/auth/login",
            json={"email": via_dashboard.email, "password": TEST_PASSWORD},
            headers={"X-Forwarded-For": "198.51.100.200"},
        )
        assert res.status_code == 200
    async with AsyncClient(
        transport=ASGITransport(app=wrapped, client=("203.0.113.66", 5000)), base_url="http://api"
    ) as c:
        res = await c.post(
            "/api/v1/auth/login",
            json={"email": direct.email, "password": TEST_PASSWORD},
            headers={"X-Forwarded-For": "6.6.6.6"},
        )
        assert res.status_code == 200

    trusted = await single_audit(
        session_factory, action="LOGIN_SUCCEEDED", actor_user_id=via_dashboard.id
    )
    untrusted = await single_audit(
        session_factory, action="LOGIN_SUCCEEDED", actor_user_id=direct.id
    )
    assert str(trusted.client_ip) == "198.51.100.200"
    assert str(untrusted.client_ip) == "203.0.113.66"
    assert untrusted.context["network"]["forwarded_for_header"] == "6.6.6.6"
