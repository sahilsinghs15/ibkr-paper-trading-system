"""Database engine and session management using SQLAlchemy 2.x and asyncpg."""

import os
import sys
from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings


def create_engine_from_settings() -> AsyncEngine:
    """Create and return an AsyncEngine instance configured with Settings."""
    settings = get_settings()
    is_testing = "pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST") is not None
    if is_testing:
        return create_async_engine(
            settings.database_url,
            echo=False,
            pool_pre_ping=True,
            poolclass=NullPool,
        )
    return create_async_engine(
        settings.database_url,
        echo=False,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        pool_timeout=30,
        pool_recycle=1800,
    )


# Default engine instance for application usage
engine: AsyncEngine = create_engine_from_settings()

# Configured async session factory
AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db_session() -> AsyncGenerator[AsyncSession, Any]:
    """Dependency for yielding an async database session."""
    async with AsyncSessionLocal() as session:
        yield session
