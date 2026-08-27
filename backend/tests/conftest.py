"""Test defaults: keep the paper STK→CFD override off unless a test enables it."""

import os
from pathlib import Path

import pytest

os.environ.setdefault("PAPER_EXECUTE_STK_AS_CFD", "false")

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from app.core.config import get_settings


@pytest.fixture
async def session_factory():
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    sf = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
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

