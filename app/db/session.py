"""Bounded async engine and request-scoped database sessions."""

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.core.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_pre_ping=True,
    )


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Release connections and roll back unfinished work when the request ends."""
    async with request.app.state.session_factory() as session:
        yield session
