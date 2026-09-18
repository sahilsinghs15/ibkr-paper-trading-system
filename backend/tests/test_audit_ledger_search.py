"""Append-only guarantees, server-side search, detail/investigation, legacy import."""

from __future__ import annotations

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.audit.context import AuditActor, RequestContext, SessionInfo
from app.audit.recorder import AuditEntry, AuditRecorder
from app.audit.security_events import reset_throttle
from app.audit.taxonomy import ActorType, AuditAction, AuditResult
from app.db.models.audit import AuditEventModel
from app.db.models.event import EventLogModel
from tests.audit_test_utils import (
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


def _entry(
    action: AuditAction,
    *,
    email: str,
    ip: str | None,
    account: str | None = None,
    summary: str = "test event",
    session_id: uuid.UUID | None = None,
    user_id: int = 1,
    role: str = "admin",
    related: dict | None = None,
) -> AuditEntry:
    session = (
        SessionInfo(
            session_id=session_id,
            created_at=datetime.now(UTC),
            expires_at=None,
            login_ip=ip,
            login_user_agent=None,
            login_device_id=None,
            login_audit_event_id=None,
        )
        if session_id
        else None
    )
    return AuditEntry(
        action=action,
        actor=AuditActor(
            actor_type=ActorType.USER,
            user_id=user_id,
            email=email,
            role=role,
            auth_method="password",
            session=session,
        ),
        summary=summary,
        request_ctx=RequestContext(request_id=uuid.uuid4().hex, client_ip=ip),
        ibkr_account=account,
        target_type="ACCOUNT",
        target_id=account,
        related=related or {},
    )


# --------------------------------------------------------------------------- immutability


async def test_finalized_rows_cannot_be_updated_or_deleted(session_factory):
    recorder = AuditRecorder(session_factory)
    event_id = await recorder.record_committed(
        _entry(AuditAction.TRADING_PAUSED, email="imm@example.com", ip="10.0.0.1")
    )
    assert event_id is not None
    for stmt in (
        "UPDATE audit_events SET actor_email = 'forged@example.com' WHERE event_id = :e",
        "UPDATE audit_events SET result = 'FAILED' WHERE event_id = :e",
        "DELETE FROM audit_events WHERE event_id = :e",
    ):
        async with session_factory() as s:
            with pytest.raises(DBAPIError, match="append-only|immutable"):
                await s.execute(text(stmt), {"e": event_id})
            await s.rollback()
    async with session_factory() as s:
        with pytest.raises(DBAPIError, match="TRUNCATE"):
            await s.execute(text("TRUNCATE audit_events"))
        await s.rollback()


async def test_pending_rows_finalize_exactly_once_and_only_outcome_columns(session_factory):
    recorder = AuditRecorder(session_factory)
    entry = _entry(AuditAction.SERVICE_RESTART, email="p@example.com", ip="10.0.0.2")
    entry.result = AuditResult.PENDING
    event_id = await recorder.record_committed(entry)

    async with session_factory() as s:
        with pytest.raises(DBAPIError, match="outcome columns"):
            await s.execute(
                text(
                    "UPDATE audit_events SET result='SUCCEEDED', actor_email='x@y.z' "
                    "WHERE event_id = :e"
                ),
                {"e": event_id},
            )
        await s.rollback()

    await recorder.finalize(
        event_id,
        result=AuditResult.ACCEPTED,
        reason="queued",
        after_state={"job": "queued"},
        related={"unit": "trading-backend.service"},
        target_id="trading-backend.service",
    )
    # Second finalize is a no-op (row no longer PENDING) and must not raise.
    await recorder.finalize(
        event_id,
        result=AuditResult.FAILED,
        reason="late",
        after_state=None,
        related={},
        target_id=None,
    )
    row = await single_audit(session_factory, event_id=event_id)
    assert row.result == "ACCEPTED" and row.result_reason == "queued"
    assert row.completed_at is not None
    assert "trading-backend.service" in row.ref_ids


async def test_record_in_transaction_rolls_back_with_business_change(session_factory):
    recorder = AuditRecorder(session_factory)
    marker = f"rollback-{uuid.uuid4().hex}"
    async with session_factory() as s:
        await recorder.record(
            s, _entry(AuditAction.MARGIN_SETTINGS_UPDATED, email=marker, ip=None)
        )
        await s.rollback()
    async with session_factory() as s:
        rows = (
            await s.execute(select(AuditEventModel).where(AuditEventModel.actor_email == marker))
        ).scalars().all()
    assert rows == []


# --------------------------------------------------------------------------- search


@pytest.fixture
async def seeded(session_factory):
    """A small, uniquely-tagged dataset spanning actors, IPs, categories and time."""
    tag = uuid.uuid4().hex[:8]
    alice = f"alice-{tag}@example.com"
    bob = f"bob-{tag}@example.com"
    acct_a = f"DUS{tag.upper()}A"
    acct_b = f"DUS{tag.upper()}B"
    sid = uuid.uuid4()
    recorder = AuditRecorder(session_factory)
    specs = [
        (AuditAction.LOGIN_SUCCEEDED, alice, "10.20.0.5", None, "login"),
        (AuditAction.ACCOUNT_SETTINGS_UPDATED, alice, "10.20.0.5", acct_a, f"risk change {tag}"),
        (AuditAction.MANUAL_ORDER_SUBMIT, alice, "10.20.0.5", acct_a, "manual BUY AAPL"),
        (AuditAction.CLOSE_PAIR, alice, "10.20.0.5", acct_a, "close pair"),
        (AuditAction.KILL_MANUAL_FLATTEN, alice, "10.20.0.5", acct_a, "kill manual"),
        (AuditAction.KILL_SWITCH_ENGINE_FLATTEN, bob, "172.16.9.9", acct_b, "kill switch"),
        (AuditAction.COMPLETE_ACCOUNT_FLATTEN, bob, "172.16.9.9", acct_b, "complete flatten"),
    ]
    ids = []
    for action, email, ip, account, summary in specs:
        entry = _entry(
            action,
            email=email,
            ip=ip,
            account=account,
            summary=summary,
            session_id=sid if email == alice else None,
            user_id=10 if email == alice else 11,
            role="admin" if email == alice else "user",
            related={"trade_id": f"TRD-{tag}"} if action is AuditAction.CLOSE_PAIR else {},
        )
        ids.append(await recorder.record_committed(entry))
    # occurred_at is the server clock at insert; read it back for range tests.
    async with session_factory() as s:
        rows = (
            await s.execute(
                select(AuditEventModel).where(AuditEventModel.event_id.in_(ids))
            )
        ).scalars().all()
    times = sorted(r.occurred_at for r in rows)
    return {
        "tag": tag,
        "alice": alice,
        "bob": bob,
        "acct_a": acct_a,
        "acct_b": acct_b,
        "sid": sid,
        "ids": ids,
        "times": times,
    }


async def _search(token: str, **params):
    async with client_for() as client:
        res = await client.get("/api/v1/audit/events", params=params, headers=auth_headers(token))
    return res


@pytest.fixture
async def admin_token(session_factory):
    user = await create_user(session_factory)
    async with client_for() as client:
        return await login(client, user)


async def test_search_combined_filters(seeded, admin_token):
    res = await _search(
        admin_token,
        actor=seeded["alice"],
        ip="10.20.0.5",
        category="EMERGENCY",
        account=seeded["acct_a"],
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total"] == 1
    assert body["items"][0]["action"] == "KILL_MANUAL_FLATTEN"
    assert body["items"][0]["client_ip"] == "10.20.0.5"


async def test_search_by_cidr_role_and_multi_action(seeded, admin_token):
    res = await _search(
        admin_token,
        ip="172.16.0.0/12",
        role="user",
        action=["KILL_SWITCH_ENGINE_FLATTEN", "COMPLETE_ACCOUNT_FLATTEN"],
        account=seeded["acct_b"],
    )
    assert res.status_code == 200
    assert {i["action"] for i in res.json()["items"]} == {
        "KILL_SWITCH_ENGINE_FLATTEN",
        "COMPLETE_ACCOUNT_FLATTEN",
    }


async def test_search_keyword_session_ref_and_time_range(seeded, admin_token):
    kw = await _search(admin_token, q=f"risk change {seeded['tag']}")
    assert [i["action"] for i in kw.json()["items"]] == ["ACCOUNT_SETTINGS_UPDATED"]

    by_session = await _search(admin_token, session_id=str(seeded["sid"]))
    assert by_session.json()["total"] == 5

    by_ref = await _search(admin_token, ref_id=f"TRD-{seeded['tag']}")
    assert [i["action"] for i in by_ref.json()["items"]] == ["CLOSE_PAIR"]

    start, end = seeded["times"][0], seeded["times"][-1]
    in_range = await _search(
        admin_token,
        actor=seeded["alice"],
        date_from=start.isoformat(),
        date_to=end.isoformat(),
    )
    assert in_range.json()["total"] == 5
    future = await _search(
        admin_token,
        actor=seeded["alice"],
        date_from=(end + timedelta(days=1)).isoformat(),
    )
    assert future.json()["total"] == 0 and future.json()["items"] == []


async def test_search_pagination_and_sorting(seeded, admin_token):
    page1 = await _search(admin_token, actor=seeded["alice"], limit=2, offset=0)
    page2 = await _search(admin_token, actor=seeded["alice"], limit=2, offset=2)
    page3 = await _search(admin_token, actor=seeded["alice"], limit=2, offset=4)
    assert page1.json()["total"] == 5
    ids = [i["event_id"] for p in (page1, page2, page3) for i in p.json()["items"]]
    assert len(ids) == 5 and len(set(ids)) == 5

    newest = await _search(admin_token, actor=seeded["alice"], sort="newest")
    oldest = await _search(admin_token, actor=seeded["alice"], sort="oldest")
    newest_ids = [i["event_id"] for i in newest.json()["items"]]
    oldest_ids = [i["event_id"] for i in oldest.json()["items"]]
    assert newest_ids == list(reversed(oldest_ids))
    assert oldest.json()["items"][0]["action"] == "LOGIN_SUCCEEDED"


@pytest.mark.parametrize(
    "params",
    [
        {"ip": "not-an-ip"},
        {"category": "NOT_A_CATEGORY"},
        {"action": "DROP_TABLE"},
        {"result": "MAYBE"},
        {"session_id": "not-a-uuid"},
        {"date_from": "2026-09-18T10:00:00Z", "date_to": "2026-09-17T10:00:00Z"},
        {"limit": 5000},
        {"sort": "sideways"},
    ],
)
async def test_search_rejects_invalid_filters(admin_token, params):
    res = await _search(admin_token, **params)
    assert res.status_code == 422, (params, res.text)


async def test_search_keyword_is_not_sql_injectable(seeded, admin_token):
    res = await _search(admin_token, q="%' OR 1=1 --", actor=seeded["alice"])
    assert res.status_code == 200
    assert res.json()["total"] == 0
    wildcard = await _search(admin_token, q="%", actor=seeded["alice"])
    assert wildcard.json()["total"] == 0  # '%' is matched literally


# --------------------------------------------------------------------------- detail


async def test_detail_view_has_changes_and_investigation_context(session_factory):
    account = await create_account(session_factory)
    user = await create_user(session_factory)
    async with client_for("198.51.100.5") as client:
        first = await login(client, user, device_id="dev-11111111-first")
    async with client_for("203.0.113.9") as client:
        second = await login(client, user, device_id="dev-22222222-second")
        res = await client.patch(
            f"/api/v1/config/accounts/{account.id}",
            json={"total_margin": 99000},
            headers=auth_headers(second, device_id="dev-22222222-second"),
        )
        assert res.status_code == 200
    row = await single_audit(
        session_factory, action="ACCOUNT_SETTINGS_UPDATED", account_id=account.id
    )
    async with client_for() as client:
        detail = await client.get(
            f"/api/v1/audit/events/{row.event_id}", headers=auth_headers(first)
        )
        missing = await client.get(
            f"/api/v1/audit/events/{uuid.uuid4()}", headers=auth_headers(first)
        )
    assert detail.status_code == 200, detail.text
    assert missing.status_code == 404
    body = detail.json()
    changes = {c["field"]: c for c in body["changes"]}
    assert set(changes) >= {"total_margin"}
    assert "updated_at" not in body["summary"]
    inv = body["investigation"]
    assert inv["session"]["login_ip"] == "203.0.113.9"
    assert inv["session"]["login_device_id"] == "dev-22222222-second"
    assert inv["session_event_count"] >= 2  # login + settings change
    concurrent = {s["login_ip"] for s in inv["concurrent_sessions"]}
    assert "198.51.100.5" in concurrent
    assert inv["device_first_seen_at"] is not None
    ips = {entry["ip"] for entry in inv["actor_ips_within_24h"]}
    assert {"198.51.100.5", "203.0.113.9"} <= ips
    assert body["context"]["identity"]["note"].startswith("Identity is the account")


async def test_facets_list_taxonomy(admin_token):
    async with client_for() as client:
        res = await client.get("/api/v1/audit/facets", headers=auth_headers(admin_token))
    assert res.status_code == 200
    body = res.json()
    cats = {c["category"]: c for c in body["categories"]}
    assert {
        "SETTINGS",
        "INVENTORY",
        "MANUAL_TRADING",
        "POSITIONS",
        "EMERGENCY",
        "SYSTEM_CONTROL",
        "AUTHENTICATION",
    } <= set(cats)
    emergency_actions = {a["action"] for a in cats["EMERGENCY"]["actions"]}
    assert {
        "KILL_SWITCH_ENGINE_FLATTEN",
        "KILL_MANUAL_FLATTEN",
        "COMPLETE_ACCOUNT_FLATTEN",
    } <= emergency_actions
    assert "PENDING" in body["results"]


# --------------------------------------------------------------------------- legacy


def _load_migration():
    path = next(
        Path(__file__).resolve().parents[1].glob("alembic/versions/a7u8d9i0t1r2_*.py")
    )
    spec = importlib.util.spec_from_file_location("audit_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_legacy_event_log_import_is_idempotent_and_never_fabricates(session_factory):
    migration = _load_migration()
    tag = uuid.uuid4().hex[:8]
    async with session_factory() as s, s.begin():
        settings_row = EventLogModel(
            process="config",
            kind="ACCOUNT_SETTINGS_CHANGED",
            detail={
                "account_id": 777,
                "ibkr_account": f"DULEG{tag}",
                "setting": "total_margin",
                "changes": {"total_margin": {"previous": "1", "new": "2"}},
                "operator": f"legacy-{tag}@example.com",
                "source": "frontend",
            },
        )
        flatten_row = EventLogModel(
            process="kill_switch",
            kind="ENGINE_POSITION_FLATTEN",
            detail={
                "account_id": 778,
                "ibkr_account": f"DULEG{tag}",
                "operation_id": str(uuid.uuid4()),
                "requested_by": "operator",
            },
        )
        machine_row = EventLogModel(
            process="rms", kind="RMS_REJECTED", detail={"ibkr_account": f"DULEG{tag}"}
        )
        s.add_all([settings_row, flatten_row, machine_row])
        await s.flush()
        ids = (settings_row.id, flatten_row.id, machine_row.id)

    for _ in range(2):  # idempotent
        async with session_factory() as s, s.begin():
            await s.execute(text(migration._IMPORT_EVENT_LOG))

    settings_audit = await single_audit(session_factory, legacy_ref=f"event_log:{ids[0]}")
    assert settings_audit.provenance == "LEGACY_EVENT_LOG"
    assert settings_audit.category == "SETTINGS"
    assert settings_audit.action == "ACCOUNT_SETTINGS_UPDATED"
    assert settings_audit.actor_email == f"legacy-{tag}@example.com"
    assert settings_audit.actor_type == "USER"
    assert settings_audit.client_ip is None and settings_audit.session_id is None
    assert settings_audit.actor_role is None

    flatten_audit = await single_audit(session_factory, legacy_ref=f"event_log:{ids[1]}")
    assert flatten_audit.action == "KILL_SWITCH_ENGINE_FLATTEN"
    assert flatten_audit.actor_type == "UNATTRIBUTED"
    assert flatten_audit.actor_email is None

    async with session_factory() as s:
        machine = (
            await s.execute(
                select(AuditEventModel).where(AuditEventModel.legacy_ref == f"event_log:{ids[2]}")
            )
        ).scalars().all()
    assert machine == []  # machine events stay in event_log only


async def test_event_journal_is_still_available_for_machine_events(session_factory):
    """The legacy event_log remains the system event journal (not the audit trail)."""
    from app.db.repositories.event_repository import EventRepository

    async with session_factory() as s:
        repo = EventRepository(s)
        await repo.append(process="rms", kind="RMS_REJECTED", detail={"account_id": 1})
        await s.commit()
        rows, total = await repo.query_events(category="risk")
    assert total >= 1
    assert any(r.kind == "RMS_REJECTED" for r in rows)


async def test_legacy_search_filter(admin_token):
    res = await _search(admin_token, provenance="native", limit=5)
    assert res.status_code == 200
    assert all(i["provenance"] == "NATIVE" for i in res.json()["items"])


async def test_explicit_correlation_and_browser_filters(session_factory, admin_token):
    tag = uuid.uuid4().hex[:8]
    recorder = AuditRecorder(session_factory)
    order_entry = _entry(
        AuditAction.MANUAL_ORDER_SUBMIT,
        email=f"corr-{tag}@example.com",
        ip="10.9.9.9",
        related={"internal_order_id": f"MAN_{tag}", "trade_id": f"TRD_{tag}"},
    )
    order_entry.target_type = "MANUAL_ORDER"
    order_entry.target_id = f"MAN_{tag}"
    order_entry.request_ctx = RequestContext(
        request_id=f"req{tag}",
        client_ip="10.9.9.9",
        client={"browser": "Firefox", "os": "Linux"},
    )
    close_entry = _entry(
        AuditAction.CLOSE_PAIR, email=f"corr-{tag}@example.com", ip="10.9.9.9"
    )
    close_entry.target_type = "POSITION"
    close_entry.target_id = f"MBG-{tag}"
    close_entry.related = {"operation_id": f"op-{tag}"}
    await recorder.record_committed(order_entry)
    await recorder.record_committed(close_entry)

    async def actions(**params):
        res = await _search(admin_token, actor=f"corr-{tag}@example.com", **params)
        assert res.status_code == 200, res.text
        return sorted(i["action"] for i in res.json()["items"])

    assert await actions(order_id=f"MAN_{tag}") == ["MANUAL_ORDER_SUBMIT"]
    assert await actions(trade_id=f"TRD_{tag}") == ["MANUAL_ORDER_SUBMIT"]
    assert await actions(position_id=f"MBG-{tag}") == ["CLOSE_PAIR"]
    assert await actions(correlation_id=f"req{tag}") == ["MANUAL_ORDER_SUBMIT"]
    assert await actions(correlation_id=f"op-{tag}") == ["CLOSE_PAIR"]
    assert await actions(browser="firefox") == ["MANUAL_ORDER_SUBMIT"]
    assert await actions(order_id=f"MAN_{tag}", trade_id="nope") == []

    async with client_for() as client:
        facets = await client.get("/api/v1/audit/facets", headers=auth_headers(admin_token))
    assert "Firefox" in facets.json()["browsers"]
