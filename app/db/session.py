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


def create_reporting_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.reporting_pool_size,
        max_overflow=0,
        pool_timeout=settings.reporting_pool_timeout,
        pool_pre_ping=True,
        connect_args={
            "server_settings": {
                "statement_timeout": str(settings.reporting_statement_timeout_ms),
                "default_transaction_read_only": "on",
            }
        },
    )


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Release connections and roll back unfinished work when the request ends."""
    async with request.app.state.session_factory() as session:
        yield session


async def get_reporting_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Use separate read-only connection capacity and release it after the request."""
    async with request.app.state.reporting_session_factory() as session:
        yield session
