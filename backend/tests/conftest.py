"""Test defaults: keep the paper STK→CFD override off unless a test enables it."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.engine import make_url

os.environ.setdefault("PAPER_EXECUTE_STK_AS_CFD", "false")
os.environ["TRADINGAPP_TESTING"] = "1"

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
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=backend_dir,
        env={**os.environ},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"alembic upgrade head failed (exit {result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}"
        )


@pytest.fixture(scope="session")
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
