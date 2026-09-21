"""Test defaults: keep the paper STK→CFD override off unless a test enables it."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import asyncpg
import pytest
from sqlalchemy.engine import make_url

os.environ.setdefault("PAPER_EXECUTE_STK_AS_CFD", "false")
os.environ["TRADINGAPP_TESTING"] = "1"
# Tests that exercise webhook auth patch get_settings(); the rest omit the header.
os.environ["WEBHOOK_AUTH_ENABLED"] = "false"

TEST_DATABASE_NAME = "ibkr_trading_test"


def _rewrite_test_database_url() -> str:
    raw = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://root:root123@localhost:5433/ibkr_trading",
    )
    url = make_url(raw)
    test_url = url.set(database=TEST_DATABASE_NAME)
    return test_url.render_as_string(hide_password=False)


os.environ["DATABASE_URL"] = _rewrite_test_database_url()

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings


async def _create_test_database_if_missing() -> None:
    settings = get_settings()
    url = make_url(settings.database_url)
    conn = await asyncpg.connect(
        host=url.host or "localhost",
        port=url.port or 5432,
        user=url.username or "postgres",
        password=url.password or "",
        database="postgres",
    )
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1",
            TEST_DATABASE_NAME,
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{TEST_DATABASE_NAME}"')
    finally:
        await conn.close()


def _run_alembic_upgrade() -> None:
    backend_dir = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "heads"],
        cwd=backend_dir,
        env={**os.environ},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"alembic upgrade heads failed (exit {result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}"
        )


@pytest.fixture(scope="session", autouse=True)
def _ensure_test_database() -> None:
    """Create ibkr_trading_test and apply Alembic migrations once per session."""
    asyncio.run(_create_test_database_if_missing())
    _run_alembic_upgrade()


@pytest.fixture
async def session_factory(_ensure_test_database):
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    sf = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    yield sf
    await engine.dispose()


@pytest.fixture(autouse=True)
def _redirect_webhook_capture_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Keep temporary webhook CSV/JSON captures out of the host data directory."""
    target_dir = tmp_path / "tradingview_webhooks"
    monkeypatch.setattr("app.api.routes.webhooks.WEBHOOK_CAPTURE_DIR", target_dir)
    return target_dir


@pytest.fixture(autouse=True)
def _clear_kill_switch_cache() -> None:  # pyrefly: ignore[bad-return]
    """Process-global kill-switch caches must not leak account ids across tests."""
    from app.services.kill_switch import (
        _KILL_SWITCH_ACTIVE_ACCOUNTS,
        _MANUAL_KILL_SWITCH_ACTIVE_ACCOUNTS,
    )

    _KILL_SWITCH_ACTIVE_ACCOUNTS.clear()
    _MANUAL_KILL_SWITCH_ACTIVE_ACCOUNTS.clear()
    yield
    _KILL_SWITCH_ACTIVE_ACCOUNTS.clear()
    _MANUAL_KILL_SWITCH_ACTIVE_ACCOUNTS.clear()


@pytest.fixture(autouse=True)
def _clear_flatten_inflight() -> None:  # pyrefly: ignore[bad-return]
    from app.services.flatten_inflight import _HELD

    _HELD.clear()
    yield
    _HELD.clear()


_APP_STATE_KEYS = ("session_factory", "order_manager")


@pytest.fixture(autouse=True)
def _restore_trading_app_state():  # pyrefly: ignore[bad-return]
    """Tests assign app.state.session_factory to a per-test engine; restore it so a
    later test never reuses a factory whose pooled connections belong to a closed
    event loop."""
    from app.main import app

    saved = {k: getattr(app.state, k) for k in _APP_STATE_KEYS if hasattr(app.state, k)}
    yield
    for key in _APP_STATE_KEYS:
        if key in saved:
            setattr(app.state, key, saved[key])
        elif hasattr(app.state, key):
            delattr(app.state, key)


@pytest.fixture(autouse=True)
def _reset_dependency_overrides():  # pyrefly: ignore[bad-return]
    """Never let a FastAPI dependency override outlive the test that set it.

    Tests that drive the app with `AsyncClient(ASGITransport(app=app))` override
    `get_db_session` with a closure over their own `session_factory`, which is
    bound to that test's event loop. Left installed, the next test to use
    `TestClient(app)` — which runs the app on a different loop in its own thread
    — gets sessions from the dead engine and fails with
    "got Future attached to a different loop". That made whole files
    (`test_position_exit_thresholds`, `test_reconcile_api`, `test_trading_pause_api`)
    pass alone but fail in a full run, depending purely on collection order.
    """
    from app.main import app

    saved = dict(app.dependency_overrides)
    yield
    app.dependency_overrides.clear()
    app.dependency_overrides.update(saved)


@pytest.fixture(autouse=True)
def _neutralize_red_zone(request):  # pyrefly: ignore[bad-return]
    """Keep execution-path tests off the wall clock.

    `OrderManager` and `WorkerPool` gate every non-emergency signal on
    `SessionClock.projected_in_red_zone()`, which is true before the RTH open,
    for `post_open_delay_seconds` after it, inside the pre-close buffer, and all
    day on a non-trading day. Tests that assert an order was routed therefore
    only passed when the suite happened to run during a live RTH session, and
    failed overnight, at weekends and on holidays.

    The real clock is restored for anything marked `@pytest.mark.real_session_clock`.
    `test_red_zone.py` is unaffected either way: it builds `SessionClock`
    instances directly with explicit datetimes rather than calling this factory.
    """
    if request.node.get_closest_marker("real_session_clock"):
        yield
        return

    from app.services import session_clock as session_clock_module

    real_factory = session_clock_module.get_session_clock

    def _open_session_clock() -> session_clock_module.SessionClock:
        # A real clock, so buffer_seconds/resolved_session_close/etc. keep their
        # production values; only the two gating predicates are forced open.
        clock = real_factory()
        clock.in_red_zone = lambda now: False  # type: ignore[method-assign]
        clock.projected_in_red_zone = lambda now: False  # type: ignore[method-assign]
        return clock

    with patch.object(session_clock_module, "get_session_clock", _open_session_clock):
        yield
