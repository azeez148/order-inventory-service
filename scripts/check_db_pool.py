"""Validate pool and session lifecycles against the configured PostgreSQL DB.

Run from the repository root:
    docker compose exec -T app python < scripts/check_db_pool.py

Fixture writes are rolled back. A backend owned by this script is terminated
to verify stale-connection recovery; PostgreSQL must allow self-session termination.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from unittest.mock import patch
from uuid import uuid4

from fastapi import Request
from pydantic import ValidationError
from sqlalchemy import event, text
from sqlalchemy.exc import TimeoutError as PoolTimeout

from app.core.config import Settings
from app.db.session import create_engine, get_session
from app.main import app, lifespan


def check_settings() -> None:
    base = {"database_url": "postgresql+asyncpg://app:test-secret@postgres/orders"}
    for override in (
        {"db_pool_size": 0},
        {"db_max_overflow": -1},
        {"db_pool_timeout": 0},
        {"db_pool_timeout": "nan"},
        {"db_pool_timeout": "inf"},
        {"database_url": "sqlite:///test-secret"},
        {"database_url": ""},
    ):
        try:
            Settings.model_validate(base | override)
        except ValidationError as error:
            assert "test-secret" not in str(error)
        else:
            raise AssertionError(f"Accepted invalid settings: {tuple(override)}")
    assert "test-secret" not in repr(Settings.model_validate(base))
    with patch.dict(os.environ, {"DB_POOL_SIZE": "2", "DB_MAX_OVERFLOW": "0", "DB_POOL_TIMEOUT": "0.5"}):
        settings = Settings.from_environment()
        assert settings.db_pool_size == 2
        assert settings.db_max_overflow == 0
        assert settings.db_pool_timeout == 0.5
    print("PASS: environment overrides, bounded settings, and redacted validation errors.")


async def check_session_cleanup() -> None:
    request = Request({"type": "http", "app": app})
    session_scope = asynccontextmanager(get_session)
    for mode in ("normal", "exception", "cancellation"):
        marker = f"pool-check-{uuid4()}"
        entered = asyncio.Event()

        async def use_session() -> None:
            async with session_scope(request) as session:
                await session.execute(
                    text("INSERT INTO products(name, price, stock) VALUES (:name, 0.10, 1)"),
                    {"name": marker},
                )
                entered.set()
                if mode == "exception":
                    raise RuntimeError("Expected validation exception")
                if mode == "cancellation":
                    await asyncio.Event().wait()

        if mode == "cancellation":
            task = asyncio.create_task(use_session())
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        elif mode == "exception":
            try:
                await use_session()
            except RuntimeError as error:
                assert str(error) == "Expected validation exception"
        else:
            await use_session()
        assert app.state.db_engine.pool.checkedout() == 0
        async with app.state.session_factory() as session:
            count = await session.scalar(
                text("SELECT count(*) FROM products WHERE name=:name"), {"name": marker}
            )
            assert count == 0, f"Uncommitted fixture persisted after {mode}"
        assert app.state.db_engine.pool.checkedout() == 0
    print("PASS: session cleanup returns connections and rolls back on normal exit, errors, and cancellation.")


async def check_pool_limits(settings: Settings) -> None:
    engine = create_engine(settings.model_copy(update={
        "db_pool_size": 1, "db_max_overflow": 1, "db_pool_timeout": 0.2
    }))
    connections = []
    try:
        for _ in range(2):
            connections.append(await engine.connect())
        assert engine.pool.checkedout() == 2
        try:
            connection = await engine.connect()
        except PoolTimeout:
            pass
        else:
            await connection.close()
            raise AssertionError("Pool allowed a third concurrent connection")
        await connections.pop().close()
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1
    finally:
        for connection in connections:
            await connection.close()
        assert engine.pool.checkedout() == 0
        await engine.dispose()
    print("PASS: pool cap, acquisition timeout, and recovery after a connection is returned.")


async def check_pre_ping(settings: Settings) -> None:
    engine = create_engine(settings.model_copy(update={"db_pool_size": 1, "db_max_overflow": 0}))
    try:
        async with engine.connect() as connection:
            old_pid = await connection.scalar(text("SELECT pg_backend_pid()"))
        async with app.state.db_engine.connect() as admin:
            assert await admin.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": old_pid})
        async with engine.connect() as connection:
            new_pid = await connection.scalar(text("SELECT pg_backend_pid()"))
            assert new_pid != old_pid
            assert await connection.scalar(text("SELECT 1")) == 1
    finally:
        await engine.dispose()
    print("PASS: pre-ping replaces this script's terminated idle connection.")


async def check_failed_startup() -> None:
    disposed = []

    def tracked_engine(settings: Settings):
        engine = create_engine(settings)
        event.listen(engine.sync_engine, "engine_disposed", lambda _: disposed.append(True))
        return engine

    with patch.dict(os.environ, {"DATABASE_URL": "postgresql+asyncpg://app@127.0.0.1:1/unavailable"}):
        with patch("app.main.create_engine", tracked_engine):
            try:
                async with lifespan(app):
                    raise AssertionError("Startup succeeded without PostgreSQL")
            except OSError:
                pass
    assert disposed == [True]
    print("PASS: failed startup disposes its engine.")


async def main() -> None:
    check_settings()
    settings = Settings.from_environment()
    disposed = []
    async with lifespan(app):
        engine = app.state.db_engine
        event.listen(engine.sync_engine, "engine_disposed", lambda _: disposed.append(True))
        await check_session_cleanup()
        await check_pool_limits(settings)
        await check_pre_ping(settings)
    assert disposed == [True]
    await check_failed_startup()
    print("PASS: database lifecycle validation complete; fixture writes were rolled back.")


if __name__ == "__main__":
    with patch.dict(os.environ, {"OUTBOX_ENABLED": "false"}):
        asyncio.run(main())
