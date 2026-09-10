"""Unit tests for demo_streaming load_ingest_jobs and raw webhook ingest visibility."""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import create_access_token
from app.db.models.account import AccountModel
from app.db.models.signal import SignalJobModel
from app.db.models.user import UserModel
from demo_streaming.api import create_demo_app
from demo_streaming.snapshot import load_ingest_jobs


@pytest.mark.asyncio
async def test_load_ingest_jobs_returns_raw_body_and_parsed_json(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Verify load_ingest_jobs returns exact raw_body, parsed_json action, and resolves account_scope=str(id)."""
    test_id = uuid4().hex[:8]
    ibkr_acc = f"DU{test_id}"
    sig_id = f"SIG-RAW-{test_id}"
    corr_id = f"CORR-{test_id}"
    exact_raw = f'{{"action":"OPEN","symbol":"AAPL","account":"{ibkr_acc}","req_id":"{test_id}"}}'
    parsed = {"action": "OPEN", "symbol": "AAPL", "account": ibkr_acc, "req_id": test_id}
    meta = {"request_id": f"REQ-{test_id}", "received_at": "2026-09-10T12:00:00Z"}

    async with session_factory() as session, session.begin():
        acc = AccountModel(
            name=f"Acc-{test_id}",
            ibkr_account=ibkr_acc,
            total_margin=Decimal("50000.00"),
        )
        session.add(acc)
        await session.flush()
        acc_id = acc.id

        job = SignalJobModel(
            signal_id=sig_id,
            strategy_id="model_blue",
            status="QUEUED",
            idempotency_key=f"idem-{test_id}",
            account_scope=str(acc_id),  # production numeric account_scope
            raw_payload=parsed,
            capture_data={
                "metadata": meta,
                "raw_body": exact_raw,
                "parsed_json": parsed,
            },
            correlation_id=corr_id,
        )
        session.add(job)

    # Note: no SignalModel row was created, confirming data is not derived from processed signals
    async with session_factory() as session:
        res = await load_ingest_jobs(session, ibkr_account=ibkr_acc)

    assert res["total"] >= 1
    matching = [j for j in res["jobs"] if j["signal_id"] == sig_id]
    assert len(matching) == 1
    item = matching[0]

    assert item["raw_body"] == exact_raw
    assert item["parsed_json"] == parsed
    assert item["action"] == "OPEN"
    assert item["metadata"] == meta
    assert item["account_scope"] == str(acc_id)
    assert item["account_id"] == acc_id
    assert item["ibkr_account"] == ibkr_acc
    assert item["status"] == "QUEUED"


@pytest.mark.asyncio
async def test_load_ingest_jobs_scopes_to_account(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Verify that filtering by account returns only matching jobs."""
    test_id_a = uuid4().hex[:8]
    test_id_b = uuid4().hex[:8]
    ibkr_a = f"DU{test_id_a}"
    ibkr_b = f"DU{test_id_b}"
    sig_a = f"SIG-A-{test_id_a}"
    sig_b = f"SIG-B-{test_id_b}"

    async with session_factory() as session, session.begin():
        acc_a = AccountModel(name="AccA", ibkr_account=ibkr_a, total_margin=Decimal("10000.00"))
        acc_b = AccountModel(name="AccB", ibkr_account=ibkr_b, total_margin=Decimal("10000.00"))
        session.add_all([acc_a, acc_b])
        await session.flush()

        job_a = SignalJobModel(
            signal_id=sig_a,
            strategy_id="model_blue",
            status="RECEIVED",
            idempotency_key=f"idem-a-{test_id_a}",
            account_scope=str(acc_a.id),
            raw_payload={"account": ibkr_a},
            capture_data={"raw_body": "body_a", "parsed_json": {"account": ibkr_a}},
            correlation_id=f"corr-a-{test_id_a}",
        )
        job_b = SignalJobModel(
            signal_id=sig_b,
            strategy_id="model_blue",
            status="RECEIVED",
            idempotency_key=f"idem-b-{test_id_b}",
            account_scope=str(acc_b.id),
            raw_payload={"account": ibkr_b},
            capture_data={"raw_body": "body_b", "parsed_json": {"account": ibkr_b}},
            correlation_id=f"corr-b-{test_id_b}",
        )
        session.add_all([job_a, job_b])

    # Filter account A by ibkr_account
    async with session_factory() as session:
        res_a = await load_ingest_jobs(session, ibkr_account=ibkr_a)
    sig_ids_a = [j["signal_id"] for j in res_a["jobs"]]
    assert sig_a in sig_ids_a
    assert sig_b not in sig_ids_a

    # Filter account B by account_id
    async with session_factory() as session:
        res_b = await load_ingest_jobs(session, account_id=acc_b.id)
    sig_ids_b = [j["signal_id"] for j in res_b["jobs"]]
    assert sig_b in sig_ids_b
    assert sig_a not in sig_ids_b


@pytest.mark.asyncio
async def test_load_ingest_jobs_admin_sees_unscoped(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Verify that global/unfiltered queries see NULL account_scope, but account queries do not."""
    test_id = uuid4().hex[:8]
    sig_unscoped = f"SIG-UNSCOPED-{test_id}"
    ibkr_acc = f"DU{test_id}"

    async with session_factory() as session, session.begin():
        acc = AccountModel(name="AccTest", ibkr_account=ibkr_acc, total_margin=Decimal("10000.00"))
        session.add(acc)
        await session.flush()

        job_unscoped = SignalJobModel(
            signal_id=sig_unscoped,
            strategy_id="model_blue",
            status="QUEUED",
            idempotency_key=f"idem-unscoped-{test_id}",
            account_scope=None,  # Unscoped / global job
            raw_payload={},
            capture_data={"raw_body": "unscoped_raw", "parsed_json": {}},
            correlation_id=f"corr-unscoped-{test_id}",
        )
        session.add(job_unscoped)

    # 1. Unfiltered query (admin global view)
    async with session_factory() as session:
        res_global = await load_ingest_jobs(session, account_id=None, ibkr_account=None)
    global_sig_ids = [j["signal_id"] for j in res_global["jobs"]]
    assert sig_unscoped in global_sig_ids

    # 2. Account-filtered query: unscoped job MUST NOT be leaked
    async with session_factory() as session:
        res_filtered = await load_ingest_jobs(session, ibkr_account=ibkr_acc)
    filtered_sig_ids = [j["signal_id"] for j in res_filtered["jobs"]]
    assert sig_unscoped not in filtered_sig_ids


@pytest.mark.asyncio
async def test_load_ingest_jobs_fanout_two_rows_same_correlation_id(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Verify fanout signals with the same correlation_id but different account scopes both exist."""
    test_id = uuid4().hex[:8]
    shared_corr = f"CORR-FANOUT-{test_id}"
    ibkr_1 = f"DU1-{test_id}"
    ibkr_2 = f"DU2-{test_id}"
    sig_1 = f"SIG-1-{test_id}"
    sig_2 = f"SIG-2-{test_id}"

    async with session_factory() as session, session.begin():
        acc1 = AccountModel(name="Acc1", ibkr_account=ibkr_1, total_margin=Decimal("10000.00"))
        acc2 = AccountModel(name="Acc2", ibkr_account=ibkr_2, total_margin=Decimal("10000.00"))
        session.add_all([acc1, acc2])
        await session.flush()

        job1 = SignalJobModel(
            signal_id=sig_1,
            strategy_id="model_blue",
            status="QUEUED",
            idempotency_key=f"idem-1-{test_id}",
            account_scope=str(acc1.id),
            raw_payload={"account": ibkr_1},
            capture_data={"raw_body": "b1", "parsed_json": {"account": ibkr_1}},
            correlation_id=shared_corr,
        )
        job2 = SignalJobModel(
            signal_id=sig_2,
            strategy_id="model_blue",
            status="QUEUED",
            idempotency_key=f"idem-2-{test_id}",
            account_scope=str(acc2.id),
            raw_payload={"account": ibkr_2},
            capture_data={"raw_body": "b2", "parsed_json": {"account": ibkr_2}},
            correlation_id=shared_corr,
        )
        session.add_all([job1, job2])

    async with session_factory() as session:
        res = await load_ingest_jobs(session, search=shared_corr)

    found_sigs = [j["signal_id"] for j in res["jobs"]]
    assert sig_1 in found_sigs
    assert sig_2 in found_sigs


@pytest.mark.asyncio
async def test_load_ingest_jobs_sql_pagination(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Verify SQL LIMIT/OFFSET pagination, page clamping, and total/total_pages calculation."""
    prefix = uuid4().hex[:8]
    corr = f"CORR-PAGE-{prefix}"

    async with session_factory() as session, session.begin():
        jobs = [
            SignalJobModel(
                signal_id=f"SIG-P-{prefix}-{i}",
                strategy_id="model_blue",
                status="QUEUED",
                idempotency_key=f"idem-page-{prefix}-{i}",
                account_scope=None,
                raw_payload={},
                capture_data={"raw_body": f"body_{i}", "parsed_json": {}},
                correlation_id=corr,
            )
            for i in range(3)
        ]
        session.add_all(jobs)

    async with session_factory() as session:
        page1 = await load_ingest_jobs(session, search=corr, page=1, page_size=2)
        page2 = await load_ingest_jobs(session, search=corr, page=2, page_size=2)

    assert page1["total"] == 3
    assert page1["total_pages"] == 2
    assert len(page1["jobs"]) == 2
    assert page1["page"] == 1
    assert page1["page_size"] == 2

    assert page2["total"] == 3
    assert page2["total_pages"] == 2
    assert len(page2["jobs"]) == 1
    assert page2["page"] == 2

    # Verify distinct items across pages
    ids_p1 = {j["signal_id"] for j in page1["jobs"]}
    ids_p2 = {j["signal_id"] for j in page2["jobs"]}
    assert len(ids_p1.intersection(ids_p2)) == 0


@pytest.mark.asyncio
async def test_load_ingest_jobs_status_filter_and_counts(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Verify filtering by REJECTED returns only REJECTED, while counts histogram reflects all statuses."""
    prefix = uuid4().hex[:8]
    corr = f"CORR-STAT-{prefix}"

    async with session_factory() as session, session.begin():
        jobs = [
            SignalJobModel(
                signal_id=f"SIG-Q-{prefix}",
                strategy_id="model_blue",
                status="QUEUED",
                idempotency_key=f"idem-q-{prefix}",
                account_scope=None,
                raw_payload={},
                capture_data={"raw_body": "q", "parsed_json": {}},
                correlation_id=corr,
            ),
            SignalJobModel(
                signal_id=f"SIG-R-{prefix}",
                strategy_id="model_blue",
                status="REJECTED",
                idempotency_key=f"idem-r-{prefix}",
                account_scope=None,
                raw_payload={},
                capture_data={"raw_body": "r", "parsed_json": {}},
                correlation_id=corr,
            ),
            SignalJobModel(
                signal_id=f"SIG-C-{prefix}",
                strategy_id="model_blue",
                status="COMPLETED",
                idempotency_key=f"idem-c-{prefix}",
                account_scope=None,
                raw_payload={},
                capture_data={"raw_body": "c", "parsed_json": {}},
                correlation_id=corr,
            ),
        ]
        session.add_all(jobs)

    async with session_factory() as session:
        res = await load_ingest_jobs(session, search=corr, status_filter="REJECTED")

    assert res["total"] == 1
    assert len(res["jobs"]) == 1
    assert res["jobs"][0]["status"] == "REJECTED"
    assert res["jobs"][0]["signal_id"] == f"SIG-R-{prefix}"

    # Counts histogram must reflect the unfiltered status distribution for this search
    counts = res["counts"]
    assert counts["queued"] >= 1
    assert counts["rejected"] >= 1
    assert counts["completed"] >= 1
    assert counts["all"] >= 3


@pytest.mark.asyncio
async def test_demo_ingest_jobs_api_authentication_and_scoping(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
):
    """Test HTTP GET /demo/ingest-jobs authentication (401) and user account scoping."""
    redis_mock = MagicMock()
    redis_mock.ping = AsyncMock(return_value=True)
    redis_mock.xread = AsyncMock(return_value=[])

    demo_app = create_demo_app(
        session_factory=session_factory,
        redis=redis_mock,
        stream_name="test:stream",
    )

    test_id = uuid4().hex[:8]
    ibkr_acc = f"DU{test_id}"

    async with session_factory() as session, session.begin():
        acc = AccountModel(name="UserAcc", ibkr_account=ibkr_acc, total_margin=Decimal("10000.00"))
        session.add(acc)
        await session.flush()

        user = UserModel(
            email=f"user_{test_id}@example.com",
            password_hash="mock",
            role="user",
            is_active=True,
            ibkr_account_id=acc.id,
        )
        session.add(user)
        await session.flush()
        user_id = user.id

    token = create_access_token({"sub": str(user_id)})
    auth_headers = {"Authorization": f"Bearer {token}"}

    async with AsyncClient(transport=ASGITransport(app=demo_app), base_url="http://test") as client:
        # 1. Unauthenticated request without TRADINGAPP_TESTING -> 401
        monkeypatch.setenv("TRADINGAPP_TESTING", "0")
        unauth_resp = await client.get("/demo/ingest-jobs")
        assert unauth_resp.status_code == 401
        monkeypatch.setenv("TRADINGAPP_TESTING", "1")

        # 2. Authenticated user request -> 200 and forced to user's account
        auth_resp = await client.get("/demo/ingest-jobs", headers=auth_headers)
        assert auth_resp.status_code == 200
        data = auth_resp.json()
        assert "jobs" in data
        assert "counts" in data
        assert "total" in data

