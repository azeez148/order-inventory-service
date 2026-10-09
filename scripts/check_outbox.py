"""Automatic HTTP synchronization and isolated worker reliability checks.

Run: docker compose exec -T app python -m scripts.check_outbox
An isolated schema tests worker failures without pausing the real services.
"""

import asyncio
from contextlib import suppress
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import httpx
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.models import Base, Product, SearchOutbox
from app.db.session import create_engine
from app.main import app, lifespan
from app.repositories.postgres_outbox import PostgreSQLSearchOutboxRepository
from app.search.outbox import SearchOutboxWorker
from app.search.meilisearch import MeilisearchProvider
from app.search.provider import SearchProduct, SearchProviderError
from scripts.fixtures import cleanup_products


class RecordingProvider:
    def __init__(self, *, fail: bool = False, blocked: bool = False) -> None:
        self.fail = fail
        self.blocked = blocked
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.products: list[SearchProduct] = []

    async def index_product(self, product: SearchProduct) -> None:
        self.entered.set()
        if self.blocked:
            await self.release.wait()
        if self.fail:
            raise SearchProviderError('Injected search outage')
        self.products.append(product)

    async def remove_product(self, product_id: int) -> None:
        raise AssertionError('Unexpected product removal')


class FailingAcknowledgement(PostgreSQLSearchOutboxRepository):
    async def mark_processed(self, event_id: int) -> None:
        await super().mark_processed(event_id)
        raise RuntimeError('Injected acknowledgement failure')


async def check_isolated(settings: Settings) -> None:
    schema = 'outbox_check_' + uuid4().hex
    admin = create_engine(settings)
    engine = create_async_engine(
        settings.database_url.get_secret_value(), pool_size=3, max_overflow=0,
        connect_args={'server_settings': {'search_path': schema}},
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def pending() -> int:
        async with sessions() as session:
            return await session.scalar(select(func.count()).select_from(SearchOutbox).where(SearchOutbox.processed_at.is_(None)))

    async def enqueue(product_id: int) -> None:
        async with sessions.begin() as session:
            session.add(SearchOutbox(product_id=product_id, event_type='upsert'))

    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as connection:
            # Test-only tables in a disposable schema; runtime still uses schema.sql.
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as session:
            product = Product(name='Before update', description='Fixture', price=Decimal('1.00'), stock=10)
            session.add(product)
            await session.flush()
            product_id = product.id
            session.add_all([SearchOutbox(product_id=product_id, event_type='upsert') for _ in range(2)])

        failure = RecordingProvider(fail=True)
        try:
            await SearchOutboxWorker(sessions, failure).process_once()
        except SearchProviderError:
            pass
        else:
            raise AssertionError('Failed indexing was acknowledged')
        assert await pending() == 2 and engine.pool.checkedout() == 0
        async with httpx.AsyncClient(base_url='http://127.0.0.1:1', timeout=0.1) as client:
            try:
                await SearchOutboxWorker(sessions, MeilisearchProvider(client)).process_once()
            except SearchProviderError:
                pass
            else:
                raise AssertionError('Unavailable search server was acknowledged')
        assert await pending() == 2 and engine.pool.checkedout() == 0
        print('PASS: injected and real network search failures leave events pending and release connections/locks.')

        blocked = RecordingProvider(blocked=True)
        worker = SearchOutboxWorker(sessions, blocked)
        task = asyncio.create_task(worker.process_once())
        try:
            await asyncio.wait_for(blocked.entered.wait(), timeout=5)
            assert not await SearchOutboxWorker(sessions, RecordingProvider()).process_once()
            async with asyncio.timeout(2):
                async with sessions.begin() as session:
                    await session.execute(update(Product).where(Product.id == product_id).values(name='Newest update', stock=11))
                    session.add(SearchOutbox(product_id=product_id, event_type='upsert'))
            assert await pending() == 3
        finally:
            blocked.release.set()
            await task
        assert await pending() == 2
        assert await worker.process_once() and await worker.process_once()
        assert not await worker.process_once()
        assert [product.name for product in blocked.products] == ['Before update', 'Newest update', 'Newest update']
        print('PASS: competing consumers serialize; product writes continue during indexing; later events project current state.')

        await enqueue(product_id)
        provider = RecordingProvider()
        with patch('app.search.outbox.PostgreSQLSearchOutboxRepository', FailingAcknowledgement):
            try:
                await SearchOutboxWorker(sessions, provider).process_once()
            except RuntimeError as error:
                assert str(error) == 'Injected acknowledgement failure'
            else:
                raise AssertionError('Injected acknowledgement failure did not propagate')
        assert await pending() == 1
        assert await SearchOutboxWorker(sessions, provider).process_once()
        assert len(provider.products) == 2 and provider.products[0] == provider.products[1]
        assert await pending() == 0
        print('PASS: indexing-before-acknowledgement failure is safely replayed and acknowledged on retry.')

        await enqueue(product_id)
        recovering = RecordingProvider(fail=True)
        retry_worker = SearchOutboxWorker(sessions, recovering, poll_interval=0.05)
        task = asyncio.create_task(retry_worker.run())
        try:
            await asyncio.wait_for(recovering.entered.wait(), timeout=5)
            assert await pending() == 1
            recovering.fail = False
            async with asyncio.timeout(5):
                while await pending():
                    await asyncio.sleep(0.05)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        assert engine.pool.checkedout() == 0
        print('PASS: the running loop retries a durable event after simulated search recovery.')

        await enqueue(product_id)
        async with sessions() as locked:
            async with locked.begin():
                row = await locked.scalar(select(SearchOutbox).where(SearchOutbox.processed_at.is_(None)).with_for_update())
                await enqueue(product_id)
                assert await SearchOutboxWorker(sessions, provider).process_once()
                assert row.processed_at is None
        assert await pending() == 1
        assert await SearchOutboxWorker(sessions, provider).process_once()
        print('PASS: a locked event is skipped without waiting; it remains retryable after release.')

        await enqueue(product_id)
        for exceptional_exit in (False, True):
            blocker = RecordingProvider(blocked=True)
            clients = []

            def factory(config, client):
                clients.append(client)
                return blocker

            with patch.dict('os.environ', {'OUTBOX_ENABLED': 'true', 'OUTBOX_POLL_INTERVAL': '0.05'}):
                with patch('app.main.create_engine', return_value=engine), patch('app.main.create_search_provider', factory):
                    try:
                        async with lifespan(app):
                            await asyncio.wait_for(blocker.entered.wait(), timeout=5)
                            if exceptional_exit:
                                raise RuntimeError('Expected lifespan failure')
                    except RuntimeError as error:
                        assert str(error) == 'Expected lifespan failure'
            assert app.state.outbox_task.done() and app.state.outbox_task.cancelled()
            assert clients[0].is_closed and engine.pool.checkedout() == 0
            assert await pending() == 1
        assert await SearchOutboxWorker(sessions, provider).process_once()
        print('PASS: normal/exceptional lifespan shutdown cancels indexing, rolls back pending intent, and closes resources.')
    finally:
        await engine.dispose()
        try:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            await admin.dispose()
    print('PASS: isolated schema removed without changing application data.')


async def check_live(settings: Settings) -> None:
    engine = create_engine(settings)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    ids = []
    marker = 'outboxcheck' + uuid4().hex

    async def wait_indexed(api: httpx.AsyncClient, query: str, product_id: int) -> None:
        async with asyncio.timeout(20):
            while True:
                async with sessions() as session:
                    remaining = await session.scalar(text(
                        'SELECT count(*) FROM search_outbox WHERE product_id=:id AND processed_at IS NULL'
                    ), {'id': product_id})
                result = await api.get('/api/v1/products/search', params={'q': query})
                if remaining == 0 and result.status_code == 200 and any(row['id'] == product_id for row in result.json()):
                    return
                await asyncio.sleep(0.1)

    try:
        async with httpx.AsyncClient(base_url='http://127.0.0.1:8000', timeout=15) as api:
            response = await api.post('/api/v1/products', json={'name': marker, 'description': 'Initial text', 'price': '1.00', 'stock': 10})
            assert response.status_code == 201, response.text
            product_id = response.json()['id']
            ids.append(product_id)
            await wait_indexed(api, marker, product_id)
            new_marker = 'updatedoutbox' + uuid4().hex
            response = await api.patch(f'/api/v1/products/{product_id}', json={'name': new_marker, 'description': 'Updated searchable text'})
            assert response.status_code == 200, response.text
            await wait_indexed(api, new_marker, product_id)
            result = await api.get('/api/v1/products/search', params={'q': marker})
            assert result.status_code == 200 and all(row['id'] != product_id for row in result.json())
            async with sessions() as session:
                events = (await session.execute(text(
                    'SELECT processed_at FROM search_outbox WHERE product_id=:id'
                ), {'id': product_id})).all()
                assert len(events) == 2 and all(row.processed_at is not None for row in events)
            print('PASS: HTTP create/update -> durable event -> automatic indexing -> processed timestamp -> updated search results.')
    finally:
        try:
            await cleanup_products(sessions, ids)
        finally:
            await engine.dispose()


async def main() -> None:
    settings = Settings.from_environment()
    assert settings.outbox_enabled, 'Enable the running app worker for the live check'
    await check_isolated(settings)
    await check_live(settings)


if __name__ == '__main__':
    asyncio.run(main())
