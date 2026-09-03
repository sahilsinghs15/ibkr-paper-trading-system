"""Unit tests for SQLAlchemy and database connection infrastructure."""

from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.db.base import Base
from app.db.session import create_engine_from_settings, get_db_session


def test_database_config() -> None:
    """Test that database_url is present in settings."""
    settings = get_settings()
    assert hasattr(settings, "database_url")
    assert settings.database_url.startswith("postgresql+asyncpg://")


def test_base_metadata() -> None:
    """Test that SQLAlchemy Base metadata is properly initialized."""
    assert Base.metadata is not None


def test_engine_creation() -> None:
    """Test that create_engine_from_settings creates an AsyncEngine."""
    test_engine = create_engine_from_settings()
    assert isinstance(test_engine, AsyncEngine)


@pytest.mark.asyncio
async def test_database_connection() -> None:
    """Test async session creation and executing a query against PostgreSQL."""
    test_engine = create_engine_from_settings()
    session_factory = async_sessionmaker(bind=test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_factory() as session:
            assert isinstance(session, AsyncSession)
            result = await session.execute(text("SELECT 1"))
            val = result.scalar()
            assert val == 1
    finally:
        await test_engine.dispose()


@pytest.mark.asyncio
async def test_get_db_session_dependency() -> None:
    """Test the get_db_session async generator dependency."""
    test_engine = create_engine_from_settings()
    factory = async_sessionmaker(bind=test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        with patch("app.db.session.AsyncSessionLocal", factory):
            async_gen = get_db_session()
            session = await anext(async_gen)
            try:
                assert isinstance(session, AsyncSession)
                result = await session.execute(text("SELECT 1"))
                assert result.scalar() == 1
            finally:
                await async_gen.aclose()
    finally:
        await test_engine.dispose()

