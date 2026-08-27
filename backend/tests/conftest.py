"""Test defaults: keep the paper STK→CFD override off unless a test enables it."""

import os

os.environ.setdefault("PAPER_EXECUTE_STK_AS_CFD", "false")

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from app.core.config import get_settings


@pytest.fixture
async def session_factory():
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    sf = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    yield sf
    await engine.dispose()

