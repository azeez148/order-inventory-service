"""Validate reporting connection isolation and statement cancellation.

Run: docker compose exec -T app python -m scripts.check_reporting_pool
No fixture writes are committed. Timeout uses a real aggregate, not pg_sleep.
"""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch

from fastapi import Request
from pydantic import ValidationError
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError, TimeoutError as PoolTimeout
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.db.session import create_engine, create_reporting_engine, get_reporting_session
from app.main import app, lifespan


def check_settings(settings: Settings) -> None:
    for override in (
        {'reporting_pool_size': 0}, {'reporting_pool_timeout': 0},
        {'reporting_statement_timeout_ms': 0}, {'reporting_pool_timeout': 'nan'},
        {'reporting_pool_size': settings.db_pool_size + settings.db_max_overflow},
    ):
        try:
            Settings.model_validate(settings.model_dump() | override)
        except ValidationError:
            pass
        else:
            raise AssertionError('Invalid/unbounded reporting settings accepted')
    print('PASS: positive finite settings and reporting capacity smaller than OLTP are enforced.')


async def check_isolation(settings: Settings) -> None:
    oltp = create_engine(settings)
    reports = create_reporting_engine(settings.model_copy(update={'reporting_pool_timeout': 0.1}))
    held = []
    try:
        for _ in range(settings.reporting_pool_size):
            held.append(await reports.connect())
        report_pids = {await connection.scalar(text('SELECT pg_backend_pid()')) for connection in held}
        try:
            extra = await reports.connect()
        except PoolTimeout:
            pass
        else:
            await extra.close()
            raise AssertionError('Reporting pool overflow was allowed')
        async with asyncio.timeout(2):
            async with oltp.connect() as connection:
                assert await connection.scalar(text('SELECT 1')) == 1
                assert await connection.scalar(text('SELECT pg_backend_pid()')) not in report_pids
                assert await connection.scalar(text('SHOW transaction_read_only')) == 'off'
        await held.pop().close()
        async with reports.connect() as connection:
            assert await connection.scalar(text('SELECT 1')) == 1
        print('PASS: report pool saturates without overflow while separate OLTP capacity remains available; acquisition recovers.')
    finally:
        for connection in held:
            await connection.close()
        assert reports.pool.checkedout() == 0 and oltp.pool.checkedout() == 0
        await reports.dispose()
        await oltp.dispose()


async def check_timeout(settings: Settings) -> None:
    reports = create_reporting_engine(settings.model_copy(update={'reporting_statement_timeout_ms': 100}))
    oltp = create_engine(settings)
    sessions = async_sessionmaker(reports, expire_on_commit=False)
    try:
        async with oltp.connect() as connection:
            original_timeout = await connection.scalar(text("SELECT setting::integer FROM pg_settings WHERE name='statement_timeout'"))
        async with sessions() as session:
            assert await session.scalar(text("SELECT setting::integer FROM pg_settings WHERE name='statement_timeout'")) == 100
            assert await session.scalar(text('SHOW transaction_read_only')) == 'on'
        try:
            async with sessions.begin() as session:
                await session.execute(text('UPDATE products SET stock = stock WHERE FALSE'))
        except DBAPIError as error:
            assert error.orig.sqlstate == '25006', error.orig.sqlstate
        else:
            raise AssertionError('Reporting connection accepted a write')
        try:
            async with sessions.begin() as session:
                await session.scalar(text('SELECT SUM(value) FROM generate_series(1, 100000000) AS value'))
        except DBAPIError as error:
            assert error.orig.sqlstate == '57014', error.orig.sqlstate
        else:
            raise AssertionError('Expensive statement did not time out')
        assert reports.pool.checkedout() == 0
        async with sessions() as session:
            assert await session.scalar(text('SELECT 1')) == 1
            assert await session.scalar(text("SELECT setting::integer FROM pg_settings WHERE name='statement_timeout'")) == 100
        async with oltp.connect() as connection:
            assert await connection.scalar(text('SHOW transaction_read_only')) == 'off'
            assert await connection.scalar(text("SELECT setting::integer FROM pg_settings WHERE name='statement_timeout'")) == original_timeout
        print('PASS: read-only writes rejected, real aggregate canceled at the test timeout, connection reusable, and OLTP settings unchanged.')
    finally:
        await reports.dispose()
        await oltp.dispose()


async def check_lifecycle() -> None:
    disposed = []
    async with lifespan(app):
        reporting = app.state.reporting_engine
        assert reporting is not app.state.db_engine
        assert reporting.pool is not app.state.db_engine.pool
        event.listen(reporting.sync_engine, 'engine_disposed', lambda _: disposed.append('reporting'))
        event.listen(app.state.db_engine.sync_engine, 'engine_disposed', lambda _: disposed.append('oltp'))
        scope = asynccontextmanager(get_reporting_session)
        async with scope(Request({'type': 'http', 'app': app})) as session:
            assert session.bind is reporting
            assert await session.scalar(text('SHOW transaction_read_only')) == 'on'
        assert reporting.pool.checkedout() == 0
    assert disposed == ['reporting', 'oltp'], disposed

    disposed = []

    def unavailable_report_engine(settings: Settings):
        unavailable = Settings.model_validate(settings.model_dump() | {
            'database_url': 'postgresql+asyncpg://app@127.0.0.1:1/unavailable'
        })
        engine = create_reporting_engine(unavailable)
        event.listen(engine.sync_engine, 'engine_disposed', lambda _: disposed.append('reporting'))
        return engine

    def tracked_oltp(settings: Settings):
        engine = create_engine(settings)
        event.listen(engine.sync_engine, 'engine_disposed', lambda _: disposed.append('oltp'))
        return engine

    with patch('app.main.create_engine', tracked_oltp), patch('app.main.create_reporting_engine', unavailable_report_engine):
        try:
            async with lifespan(app):
                raise AssertionError('Startup accepted unavailable reporting connection')
        except OSError:
            pass
    assert disposed == ['reporting', 'oltp'], disposed
    print('PASS: reporting dependency binds its own engine; startup failure and shutdown dispose both pools.')


async def main() -> None:
    settings = Settings.from_environment()
    check_settings(settings)
    await check_isolation(settings)
    await check_timeout(settings)
    await check_lifecycle()


if __name__ == '__main__':
    with patch.dict('os.environ', {'OUTBOX_ENABLED': 'false'}):
        asyncio.run(main())
