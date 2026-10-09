"""Product HTTP and transactional-outbox checks against the running app.

Run: docker compose exec -T app python < scripts/check_products.py
Only script-owned products and their outbox events are removed.
"""

import asyncio
import json
from decimal import Decimal
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.db.session import create_engine
from app.repositories.postgres_products import PostgreSQLProductRepository
from app.schemas.products import ProductCreate, ProductPatch
from app.services.products import ProductService


def request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode()
    message = Request('http://127.0.0.1:8000' + path, data=data, method=method,
                      headers={'Content-Type': 'application/json'})
    try:
        with urlopen(message, timeout=15) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


async def http(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    return await asyncio.to_thread(request, method, path, payload)


class FailingOutboxRepository(PostgreSQLProductRepository):
    async def enqueue_search_update(self, product_id: int) -> None:
        await super().enqueue_search_update(product_id)
        raise RuntimeError('Injected outbox failure')


async def main() -> None:
    engine = create_engine(Settings.from_environment())
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    ids = []
    root = '/api/v1/products'
    marker = f'product-check-{uuid4()}'

    async def events(product_id: int) -> int:
        async with sessions() as session:
            rows = (await session.execute(
                text('SELECT event_type, processed_at FROM search_outbox WHERE product_id=:id'),
                {'id': product_id},
            )).all()
            assert all(row.event_type == 'upsert' and row.processed_at is None for row in rows)
            return len(rows)

    try:
        status, product = await http('POST', root, {'name': marker, 'price': '19.99', 'stock': 10})
        assert status == 201, product
        product_id = product['id']
        ids.append(product_id)
        path = f'{root}/{product_id}'
        assert Decimal(product['price']) == Decimal('19.99') and product['stock'] == 10
        assert await events(product_id) == 1
        status, retrieved = await http('GET', path)
        assert status == 200 and retrieved == product
        status, updated = await http('PATCH', path, {'name': marker + '-new', 'description': 'Searchable description'})
        assert status == 200 and updated['name'] == marker + '-new'
        assert updated['updated_at'] > product['updated_at']
        assert await events(product_id) == 2
        status, no_op = await http('PATCH', path, {'name': marker + '-new'})
        assert status == 200 and no_op == updated
        assert await events(product_id) == 2
        status, priced = await http('PATCH', path, {'price': '24.99'})
        assert status == 200 and Decimal(priced['price']) == Decimal('24.99')
        assert await events(product_id) == 2
        for adjustment, expected in ((25, 35), (-5, 30)):
            status, adjusted = await http('POST', path + '/stock', {'adjustment': adjustment})
            assert status == 200 and adjusted['stock'] == expected
        assert (await http('POST', path + '/stock', {'adjustment': -31}))[0] == 409
        assert (await http('GET', path))[1]['stock'] == 30
        assert await events(product_id) == 2
        print('PASS: create/get/patch, exact prices, stock changes, no-op patches, and outbox counts.')

        for payload in (
            {'name': ' ', 'price': '1'}, {'name': marker, 'price': '-1'},
            {'name': marker, 'price': '1.001'}, {'name': marker, 'price': '10000000000'},
            {'name': marker, 'price': 'NaN'}, {'name': marker, 'price': 'Infinity'},
            {'name': marker, 'price': '1', 'stock': -1},
            {'name': marker, 'price': '1', 'stock': 2147483648},
            {'name': marker, 'price': '1', 'stock': True},
        ):
            assert (await http('POST', root, payload))[0] == 422, payload
        for payload in ({}, {'name': None}, {'description': None}, {'price': None}, {'stock': 4}):
            assert (await http('PATCH', path, payload))[0] == 422, payload
        for adjustment in (0, True, 1.5, 2147483648):
            assert (await http('POST', path + '/stock', {'adjustment': adjustment}))[0] == 422
        missing = root + '/9223372036854775807'
        assert (await http('GET', missing))[0] == 404
        assert (await http('PATCH', missing, {'name': 'Missing'}))[0] == 404
        assert (await http('POST', missing + '/stock', {'adjustment': 1}))[0] == 404
        assert (await http('GET', root + '/0'))[0] == 422
        assert (await http('GET', root + '/9223372036854775808'))[0] == 422
        print('PASS: invalid fields/precision/ranges return 422; missing products return 404; invalid stock state returns 409.')

        results = await asyncio.gather(*(http('POST', path + '/stock', {'adjustment': 1}) for _ in range(30)))
        assert all(status == 200 for status, _ in results), results
        assert (await http('GET', path))[1]['stock'] == 60
        assert (await http('POST', path + '/stock', {'adjustment': -59}))[0] == 200
        results = await asyncio.gather(*(http('POST', path + '/stock', {'adjustment': -1}) for _ in range(2)))
        assert sorted(status for status, _ in results) == [200, 409], results
        assert (await http('GET', path))[1]['stock'] == 0
        assert await events(product_id) == 2
        status, maximum = await http('POST', root, {'name': marker + '-max', 'price': '0.30', 'stock': 2147483647})
        assert status == 201, maximum
        ids.append(maximum['id'])
        assert (await http('POST', f"{root}/{maximum['id']}/stock", {'adjustment': 1}))[0] == 409
        print('PASS: concurrent increments have no lost updates, competing decrements cannot oversell, and overflow is rejected.')

        for operation in ('create', 'update'):
            async with sessions() as session:
                service = ProductService(session, FailingOutboxRepository(session))
                try:
                    if operation == 'create':
                        await service.create(ProductCreate(name=marker + '-rollback', price=Decimal('1.00')))
                    else:
                        await service.update(product_id, ProductPatch(name=marker + '-rollback'))
                except RuntimeError as error:
                    assert str(error) == 'Injected outbox failure'
                else:
                    raise AssertionError('Injected failure was not propagated')
            async with sessions() as session:
                assert await session.scalar(text('SELECT count(*) FROM products WHERE name=:name'), {'name': marker + '-rollback'}) == 0
            assert await events(product_id) == 2
        assert (await http('GET', path))[1]['name'] == marker + '-new'
        print('PASS: injected outbox failures roll back product create/update and outbox insertion together.')
    finally:
        try:
            if ids:
                async with sessions.begin() as session:
                    await session.execute(text('DELETE FROM search_outbox WHERE product_id=ANY(:ids)'), {'ids': ids})
                    await session.execute(text('DELETE FROM products WHERE id=ANY(:ids)'), {'ids': ids})
        finally:
            await engine.dispose()
    print('PASS: product checks complete; script-owned fixtures and events removed.')


if __name__ == '__main__':
    asyncio.run(main())
