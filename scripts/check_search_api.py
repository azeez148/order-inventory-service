"""Search HTTP/hydration checks; only this script's fixture data is removed.

Run: docker compose exec -T app python < scripts/check_search_api.py
Controlled provider snapshots simulate index lag while the live consumer runs.
"""

import asyncio
from decimal import Decimal
from uuid import uuid4

import httpx
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.products import get_search_service
from app.core.config import Settings
from app.db.session import create_engine
from app.main import app
from app.repositories.postgres_products import PostgreSQLProductRepository
from app.search.factory import create_search_provider
from app.search.provider import SearchProduct, SearchProviderError
from app.services.search import ProductSearchService
from scripts.fixtures import cleanup_products


class FixedProvider:
    def __init__(self, ids: list[int]) -> None:
        self.ids = ids

    async def search_products(self, query: str, limit: int = 20) -> list[int]:
        return self.ids


class UnavailableProvider:
    async def search_products(self, query: str, limit: int = 20) -> list[int]:
        raise SearchProviderError('Private bearer key and server details')


class UnusedRepository:
    async def get_many(self, ids: list[int]):
        raise AssertionError('Database hydration should not run here')


async def main() -> None:
    settings = Settings.from_environment()
    engine = create_engine(settings)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    key = settings.meilisearch_master_key.get_secret_value()
    ids = []
    marker = 'searchcheck' + uuid4().hex
    async with httpx.AsyncClient(
        base_url=str(settings.meilisearch_url),
        headers={'Authorization': f'Bearer {key}'} if key else {},
        timeout=settings.search_http_timeout,
    ) as search_client, httpx.AsyncClient(base_url='http://127.0.0.1:8000', timeout=20) as api, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url='http://snapshot',
    ) as snapshot_api:
        provider = create_search_provider(settings, search_client)
        try:
            for name, description in (
                (marker + ' notebook', 'Paper stationery'),
                ('Sports glove', marker + ' protection'),
                (marker + ' missing fixture', 'Stale document'),
            ):
                response = await api.post('/api/v1/products', json={
                    'name': name, 'description': description, 'price': '19.99', 'stock': 10,
                })
                assert response.status_code == 201, response.text
                product = response.json()
                ids.append(product['id'])
                await provider.index_product(SearchProduct(product['id'], name, description))
            first, second, stale = ids
            expected = await provider.search_products(marker)
            response = await api.get('/api/v1/products/search', params={'q': marker})
            assert response.status_code == 200, response.text
            assert [product['id'] for product in response.json()] == expected
            assert len(response.json()) == 3
            assert (await api.get(f'/api/v1/products/{first}')).status_code == 200
            response = await api.get('/api/v1/products/search', params={'q': marker, 'limit': 1})
            assert response.status_code == 200 and len(response.json()) == 1
            for params in ({}, {'q': ''}, {'q': '  '}, {'q': 'x' * 201}, {'q': marker, 'limit': 0}, {'q': marker, 'limit': 101}):
                response = await api.get('/api/v1/products/search', params=params)
                assert response.status_code == 422, (params, response.text)
            response = await api.get('/api/v1/products/search', params={'q': '  ' + marker + '  '})
            assert response.status_code == 200 and len(response.json()) == 3
            response = await api.get('/api/v1/products/search', params={'q': 'noresult' + uuid4().hex})
            assert response.status_code == 200 and response.json() == []
            print('PASS: real search endpoint, route precedence, name/description matches, relevance, limits, and query validation.')

            assert (await api.patch(f'/api/v1/products/{first}', json={'name': 'Renamed product', 'price': '24.99'})).status_code == 200
            assert (await api.post(f'/api/v1/products/{first}/stock', json={'adjustment': -3})).status_code == 200
            app.state.session_factory = sessions
            app.state.search_provider = FixedProvider(expected)
            response = await snapshot_api.get('/api/v1/products/search', params={'q': marker})
            hydrated = next(product for product in response.json() if product['id'] == first)
            assert hydrated['name'] == 'Renamed product'
            assert Decimal(hydrated['price']) == Decimal('24.99') and hydrated['stock'] == 7
            async with sessions.begin() as session:
                await session.execute(text('DELETE FROM search_outbox WHERE product_id=:id'), {'id': stale})
                await session.execute(text('DELETE FROM products WHERE id=:id'), {'id': stale})
            response = await snapshot_api.get('/api/v1/products/search', params={'q': marker})
            assert response.status_code == 200
            assert [product['id'] for product in response.json()] == [product_id for product_id in expected if product_id != stale]
            print('PASS: stale text still yields authoritative name/price/stock; missing DB products are omitted.')

            statements = []

            def capture(connection, cursor, statement, parameters, context, executemany):
                statements.append(statement)

            event.listen(engine.sync_engine, 'before_cursor_execute', capture)
            try:
                async with sessions() as session:
                    repository = PostgreSQLProductRepository(session)
                    service = ProductSearchService(FixedProvider([second, stale, first]), repository)
                    results = await service.search(marker)
                    assert [product.id for product in results] == [second, first]
                assert len(statements) == 1, statements
                assert await ProductSearchService(FixedProvider([]), UnusedRepository()).search(marker) == []
                assert len(statements) == 1
            finally:
                event.remove(engine.sync_engine, 'before_cursor_execute', capture)
            print('PASS: hydration uses one SQL query, restores provider order, and skips DB access on empty matches.')

            async with asyncio.timeout(15):
                while True:
                    async with sessions() as session:
                        pending = await session.scalar(text(
                            'SELECT count(*) FROM search_outbox WHERE product_id=:id AND processed_at IS NULL'
                        ), {'id': first})
                    if pending == 0:
                        break
                    await asyncio.sleep(0.05)
            await provider.index_product(SearchProduct(first, 'Renamed product', 'Paper stationery'))
            response = await api.get('/api/v1/products/search', params={'q': marker})
            assert response.status_code == 200 and [product['id'] for product in response.json()] == [second]

            async def unavailable_service():
                return ProductSearchService(UnavailableProvider(), UnusedRepository())

            app.dependency_overrides[get_search_service] = unavailable_service
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    response = await client.get('/api/v1/products/search', params={'q': marker})
                    assert response.status_code == 503
                    assert response.json() == {'detail': 'Search is temporarily unavailable'}
            finally:
                app.dependency_overrides.pop(get_search_service, None)
            print('PASS: explicit projection refresh changes matches; provider failure maps to sanitized HTTP 503 without SQL fallback.')
        finally:
            try:
                await cleanup_products(sessions, ids)
            finally:
                await engine.dispose()
    print('PASS: search API validation complete; only script-owned documents/products/events removed.')


if __name__ == '__main__':
    asyncio.run(main())
