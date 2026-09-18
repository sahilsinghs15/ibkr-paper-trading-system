"""Actor attribution, sessions, authentication/authorization audit, sanitization."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import jwt
import pytest
from sqlalchemy import select

from app.audit.context import parse_user_agent
from app.audit.sanitize import (
    MAX_PAYLOAD_BYTES,
    REDACTED,
    sanitize_dict,
    sanitize_payload,
)
from app.audit.security_events import reset_throttle
from app.core.config import get_settings
from app.db.models.audit import AuthSessionModel
from tests.audit_test_utils import (
    CHROME_WINDOWS_UA,
    FIREFOX_MAC_UA,
    TEST_PASSWORD,
    audit_rows,
    auth_headers,
    client_for,
    create_account,
    create_user,
    install_app_state,
    login,
    single_audit,
)


@pytest.fixture(autouse=True)
def _state(session_factory):
    install_app_state(session_factory)
    reset_throttle()
    yield
    reset_throttle()


# --------------------------------------------------------------------------- sanitize


def test_sanitize_redacts_secrets_by_key_and_by_value():
    payload = {
        "password": "hunter2",
        "nested": {"api_key": "abc", "Authorization": "Bearer xyz", "sse_token": "t"},
        "note": "header was Bearer abcdefghijklmnop and jwt eyJhbGciOi.eyJzdWIiOiIx.c2lnbmF0dXJl",
        "idempotency_key": "keep-me",
        "items": [{"client_secret": "s"}],
    }
    out = sanitize_dict(payload)
    assert out["password"] == REDACTED
    assert out["nested"]["api_key"] == REDACTED
    assert out["nested"]["Authorization"] == REDACTED
    assert out["nested"]["sse_token"] == REDACTED
    assert out["items"][0]["client_secret"] == REDACTED
    assert out["idempotency_key"] == "keep-me"
    assert "abcdefghijklmnop" not in out["note"]
    assert "eyJzdWIiOiIx" not in out["note"]


def test_sanitize_strips_control_chars_bounds_size_and_keeps_decimal_precision():
    out = sanitize_dict(
        {
            "evil": "line1\r\nFAKE LOG ENTRY\x1b[31m",
            "long": "x" * 5000,
            "qty": Decimal("0.00012345"),
        }
    )
    assert "\n" not in out["evil"] and "\x1b" not in out["evil"]
    assert len(out["long"]) < 1200 and "truncated" in out["long"]
    assert out["qty"] == "0.00012345"

    huge = sanitize_payload({f"k{i}": "y" * 900 for i in range(90)})
    assert huge["_truncated"] is True
    assert huge["_original_bytes"] > MAX_PAYLOAD_BYTES


def test_user_agent_parsing_is_coarse_and_non_invasive():
    chrome = parse_user_agent(CHROME_WINDOWS_UA)
    assert chrome["browser"] == "Chrome" and chrome["os"] == "Windows"
    assert chrome["device_class"] == "desktop"
    firefox = parse_user_agent(FIREFOX_MAC_UA)
    assert firefox["browser"] == "Firefox" and firefox["os"] == "macOS"
    iphone = parse_user_agent(
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
    )
    assert iphone["browser"] == "Safari" and iphone["os"] == "iOS"
    assert iphone["device_class"] == "mobile"
    assert parse_user_agent("curl/8.5.0")["device_class"] == "api-client"
    assert parse_user_agent(None)["browser"] is None


# --------------------------------------------------------------------------- login / session


async def test_login_creates_server_session_and_audits_with_actor_context(session_factory):
    user = await create_user(session_factory)
    async with client_for("198.51.100.23") as client:
        token = await login(client, user)

    claims = jwt.decode(token, get_settings().jwt_secret_key, algorithms=["HS256"])
    sid = uuid.UUID(claims["sid"])

    async with session_factory() as s:
        auth_session = await s.get(AuthSessionModel, sid)
    assert auth_session is not None
    assert auth_session.user_id == user.id
    assert str(auth_session.login_ip) == "198.51.100.23"
    assert auth_session.login_device_id == "dev-4f0c2a9e-1111-2222"
    assert auth_session.login_user_agent == CHROME_WINDOWS_UA
    assert auth_session.ended_at is None

    row = await single_audit(session_factory, action="LOGIN_SUCCEEDED", actor_user_id=user.id)
    assert row.category == "AUTHENTICATION"
    assert row.result == "SUCCEEDED"
    assert row.actor_email == user.email
    assert row.actor_role == "admin"
    assert row.session_id == sid
    assert str(row.client_ip) == "198.51.100.23"
    assert row.client_device_id == "dev-4f0c2a9e-1111-2222"
    assert row.context["client"]["browser"] == "Chrome"
    assert row.context["client"]["client_reported_fields_are_unverified"] is True
    assert auth_session.login_audit_event_id == row.event_id
    assert row.request_id  # server generated


@pytest.mark.parametrize("reason", ["BAD_PASSWORD", "UNKNOWN_ACCOUNT"])
async def test_failed_login_is_audited_without_password(session_factory, reason):
    user = await create_user(session_factory)
    email = user.email if reason == "BAD_PASSWORD" else f"nobody-{uuid.uuid4().hex[:8]}@example.com"
    secret_guess = f"guess-{uuid.uuid4().hex}"
    async with client_for("192.0.2.99") as client:
        res = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": secret_guess}
        )
    assert res.status_code == 401

    row = await single_audit(session_factory, action="LOGIN_FAILED", target_id=email)
    assert row.actor_type == "ANONYMOUS"
    assert row.actor_user_id is None and row.actor_email is None
    assert row.result == "DENIED"
    assert row.result_reason == reason
    assert str(row.client_ip) == "192.0.2.99"
    serialized = json.dumps(
        [row.parameters, row.context, row.summary, row.result_reason], default=str
    )
    assert secret_guess not in serialized
    assert TEST_PASSWORD not in serialized


async def test_logout_ends_session_and_token_reuse_is_rejected_and_audited(session_factory):
    user = await create_user(session_factory)
    async with client_for() as client:
        token = await login(client, user)
        me = await client.get("/api/v1/auth/me", headers=auth_headers(token))
        assert me.status_code == 200

        out = await client.post("/api/v1/auth/logout", headers=auth_headers(token))
        assert out.status_code == 200 and out.json()["session_ended"] is True

        replay = await client.get("/api/v1/auth/me", headers=auth_headers(token))
        assert replay.status_code == 401

    sid = uuid.UUID(jwt.decode(token, options={"verify_signature": False})["sid"])
    async with session_factory() as s:
        auth_session = await s.get(AuthSessionModel, sid)
    assert auth_session.ended_at is not None and auth_session.end_reason == "LOGOUT"

    logout_row = await single_audit(session_factory, action="LOGOUT", actor_user_id=user.id)
    assert logout_row.session_id == sid
    rejected = await single_audit(
        session_factory, action="SESSION_TOKEN_REJECTED", target_id=str(sid)
    )
    assert rejected.result == "DENIED"
    assert "LOGOUT" in (rejected.result_reason or "")


async def test_ended_session_token_rejection_is_throttled(session_factory):
    user = await create_user(session_factory)
    async with client_for() as client:
        token = await login(client, user)
        await client.post("/api/v1/auth/logout", headers=auth_headers(token))
        for _ in range(5):
            res = await client.get("/api/v1/auth/me", headers=auth_headers(token))
            assert res.status_code == 401
    sid = jwt.decode(token, options={"verify_signature": False})["sid"]
    rows = await audit_rows(session_factory, action="SESSION_TOKEN_REJECTED", target_id=sid)
    assert len(rows) == 1


async def test_sse_token_is_bound_to_session(session_factory):
    user = await create_user(session_factory)
    async with client_for() as client:
        token = await login(client, user)
        res = await client.post("/api/v1/auth/sse-token", headers=auth_headers(token))
    assert res.status_code == 200
    sse_claims = jwt.decode(res.json()["sse_token"], options={"verify_signature": False})
    assert sse_claims["sid"] == jwt.decode(token, options={"verify_signature": False})["sid"]


# --------------------------------------------------------------------------- attribution


async def test_frontend_cannot_spoof_identity_or_ip(session_factory):
    """Body fields and forwarding headers never override server-derived identity."""
    account = await create_account(session_factory)
    user = await create_user(session_factory)
    victim = await create_user(session_factory)
    async with client_for("203.0.113.44") as client:
        token = await login(client, user)
        res = await client.patch(
            f"/api/v1/config/accounts/{account.id}",
            json={
                "total_margin": 123456,
                "user_id": victim.id,
                "email": victim.email,
                "role": "admin",
                "actor_email": victim.email,
            },
            headers=auth_headers(
                token,
                **{
                    "X-Forwarded-For": "6.6.6.6",
                    "X-Real-IP": "7.7.7.7",
                    "X-User-Email": victim.email,
                    "X-Request-ID": "client-chosen-id",
                },
            ),
        )
    assert res.status_code == 200, res.text

    row = await single_audit(
        session_factory, action="ACCOUNT_SETTINGS_UPDATED", account_id=account.id
    )
    assert row.actor_user_id == user.id
    assert row.actor_email == user.email
    # Peer is not a trusted proxy in this test, so the forwarded header is only evidence.
    assert str(row.client_ip) == "203.0.113.44"
    assert row.context["network"]["forwarded_for_header"] == "6.6.6.6"
    assert row.request_id != "client-chosen-id"
    assert row.context["client"]["client_correlation_id"] is None
    assert "user_id" not in row.parameters and "actor_email" not in row.parameters


async def test_session_observations_flag_context_change_since_login(session_factory):
    account = await create_account(session_factory)
    user = await create_user(session_factory)
    async with client_for("198.51.100.1") as client:
        token = await login(client, user, device_id="dev-aaaaaaaa-original")
    # Same token, different network + browser install + user agent.
    async with client_for("203.0.113.200") as other:
        res = await other.put(
            f"/api/v1/config/accounts/{account.id}/default-symbol-limit",
            json={"default_symbol_limit": 42000},
            headers=auth_headers(token, user_agent=FIREFOX_MAC_UA, device_id="dev-bbbbbbbb-unknown"),
        )
    assert res.status_code == 200, res.text
    row = await single_audit(
        session_factory, action="DEFAULT_SYMBOL_LIMIT_UPDATED", account_id=account.id
    )
    obs = row.context["session_observations"]
    assert obs["ip_matches_login"] is False
    assert obs["device_matches_login"] is False
    assert obs["user_agent_matches_login"] is False
    assert obs["login_ip"] == "198.51.100.1"
    assert obs["session_age_seconds"] >= 0


async def test_invalid_device_id_header_is_discarded(session_factory):
    account = await create_account(session_factory)
    user = await create_user(session_factory)
    async with client_for() as client:
        token = await login(client, user)
        res = await client.put(
            f"/api/v1/config/accounts/{account.id}/default-symbol-limit",
            json={"default_symbol_limit": 1000},
            headers=auth_headers(token, device_id="<script>alert(1)</script>"),
        )
    assert res.status_code == 200
    row = await single_audit(
        session_factory, action="DEFAULT_SYMBOL_LIMIT_UPDATED", account_id=account.id
    )
    assert row.client_device_id is None
    assert row.context["client"]["device_id_invalid"] is True


# --------------------------------------------------------------------------- authorization


async def test_non_admin_cannot_read_audit_and_denial_is_audited(session_factory):
    account = await create_account(session_factory)
    user = await create_user(session_factory, role="user", account_id=account.id)
    async with client_for() as client:
        token = await login(client, user)
        res = await client.get("/api/v1/audit/events", headers=auth_headers(token))
        detail = await client.get(
            f"/api/v1/audit/events/{uuid.uuid4()}", headers=auth_headers(token)
        )
    assert res.status_code == 403
    assert detail.status_code == 403
    rows = await audit_rows(session_factory, action="ACCESS_DENIED", actor_user_id=user.id)
    assert rows, "authorization denial should be audited"
    assert rows[0].actor_role == "user"
    assert rows[0].target_id == "GET /api/v1/audit/events"


async def test_cross_account_mutation_is_denied_and_audited(session_factory):
    own = await create_account(session_factory)
    other = await create_account(session_factory)
    user = await create_user(session_factory, role="user", account_id=own.id)
    async with client_for() as client:
        token = await login(client, user)
        res = await client.post(
            f"/api/v1/config/accounts/{other.id}/trading-pause", headers=auth_headers(token)
        )
    assert res.status_code == 403
    denied = await audit_rows(session_factory, action="ACCESS_DENIED", actor_user_id=user.id)
    assert len(denied) == 1
    assert await audit_rows(session_factory, action="TRADING_PAUSED", account_id=other.id) == []


async def test_audit_api_exposes_no_mutation_routes(session_factory):
    user = await create_user(session_factory)
    async with client_for() as client:
        token = await login(client, user)
        some_id = uuid.uuid4()
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            res = await client.request(
                method, f"/api/v1/audit/events/{some_id}", headers=auth_headers(token)
            )
            assert res.status_code == 405, (method, res.status_code)
        res = await client.request("DELETE", "/api/v1/audit/events", headers=auth_headers(token))
        assert res.status_code == 405


async def test_login_session_rows_are_isolated_per_login(session_factory):
    user = await create_user(session_factory)
    async with client_for() as client:
        t1 = await login(client, user)
        t2 = await login(client, user)
    sid1 = jwt.decode(t1, options={"verify_signature": False})["sid"]
    sid2 = jwt.decode(t2, options={"verify_signature": False})["sid"]
    assert sid1 != sid2
    async with session_factory() as s:
        rows = (
            await s.execute(select(AuthSessionModel).where(AuthSessionModel.user_id == user.id))
        ).scalars().all()
    assert len(rows) == 2
