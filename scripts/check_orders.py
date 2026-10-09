"""Order HTTP/transaction checks against the running app and database.

Run: docker compose exec -T app python < scripts/check_orders.py
Only this script's fixture orders, products, and outbox events are removed.
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
from app.repositories.postgres_inventory import PostgreSQLInventoryRepository
from app.repositories.postgres_orders import PostgreSQLOrderRepository
from app.repositories.orders import NewOrderItem, OrderRecord
from app.schemas.orders import OrderCreate
from app.services.orders import OrderService
from scripts.fixtures import cleanup_products


async def http(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    def request() -> tuple[int, dict]:
        message = Request(
            'http://127.0.0.1:8000' + path, method=method,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json'},
        )
        try:
            with urlopen(message, timeout=15) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)
    return await asyncio.to_thread(request)


class FailingOrderRepository(PostgreSQLOrderRepository):
    async def create(self, total_amount: Decimal, items: list[NewOrderItem]) -> OrderRecord:
        await super().create(total_amount, items)
        raise RuntimeError('Injected order persistence failure')


async def main() -> None:
    engine = create_engine(Settings.from_environment())
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    products = []
    orders = []
    prefix = f'order-check-{uuid4()}'

    async def product(stock: int, price: str = '19.99') -> int:
        status, body = await http('POST', '/api/v1/products', {
            'name': prefix + f'-{len(products)}', 'price': price, 'stock': stock,
        })
        assert status == 201, body
        products.append(body['id'])
        return body['id']

    async def place(items: list[dict]) -> tuple[int, dict]:
        status, body = await http('POST', '/api/v1/orders', {'items': items})
        if status == 201:
            orders.append(body['id'])
        return status, body

    async def stock(product_id: int) -> int:
        status, body = await http('GET', f'/api/v1/products/{product_id}')
        assert status == 200, body
        return body['stock']

    async def order_count() -> int:
        async with sessions() as session:
            return await session.scalar(text(
                'SELECT count(DISTINCT order_id) FROM order_items WHERE product_id=ANY(:ids)'
            ), {'ids': products})

    try:
        race = await product(1, '0.30')
        results = await asyncio.gather(*(place([{'product_id': race, 'quantity': 1}]) for _ in range(2)))
        assert sorted(status for status, _ in results) == [201, 409], results
        assert await stock(race) == 0
        print('PASS: two concurrent HTTP orders against stock=1 yield one 201, one 409, final stock=0.')

        first = await product(10, '0.10')
        second = await product(1, '0.20')
        count = await order_count()
        assert (await place([{'product_id': first, 'quantity': 5}, {'product_id': second, 'quantity': 5}]))[0] == 409
        assert await stock(first) == 10 and await stock(second) == 1
        assert await order_count() == count
        assert (await place([{'product_id': first, 'quantity': 5}, {'product_id': 9223372036854775807, 'quantity': 1}]))[0] == 404
        assert await stock(first) == 10 and await order_count() == count
        status, created = await place([
            {'product_id': second, 'quantity': 1},
            {'product_id': first, 'quantity': 2},
            {'product_id': first, 'quantity': 3},
        ])
        assert status == 201, created
        assert created['status'] == 'created' and Decimal(created['total_amount']) == Decimal('0.70')
        assert [item['product_id'] for item in created['items']] == [first, second]
        assert [item['quantity'] for item in created['items']] == [5, 1]
        assert await stock(first) == 5 and await stock(second) == 0
        assert (await http('GET', f"/api/v1/orders/{created['id']}")) == (200, created)
        assert (await http('PATCH', f'/api/v1/products/{first}', {'price': '9.99'}))[0] == 200
        assert (await http('GET', f"/api/v1/orders/{created['id']}"))[1] == created
        print('PASS: insufficient/missing-item rollback, duplicate normalization, sorted items, decimal totals, retrieval, and price snapshots.')

        count = await order_count()
        for payload in (
            {}, {'items': []}, {'items': [{'product_id': first, 'quantity': 0}]},
            {'items': [{'product_id': first, 'quantity': -1}]},
            {'items': [{'product_id': first, 'quantity': True}]},
            {'items': [{'product_id': first, 'quantity': 1.5}]},
            {'items': [{'product_id': 0, 'quantity': 1}]},
            {'items': [{'product_id': first, 'quantity': 2147483648}]},
            {'items': [{'product_id': first, 'quantity': 2147483647}, {'product_id': first, 'quantity': 1}]},
            {'items': [{'product_id': first, 'quantity': 1}] * 101},
            {'items': [{'product_id': first, 'quantity': 1, 'unit_price': '0.01'}]},
        ):
            assert (await http('POST', '/api/v1/orders', payload))[0] == 422, payload
        assert await stock(first) == 5 and await order_count() == count
        assert (await http('GET', '/api/v1/orders/9223372036854775807'))[0] == 404
        assert (await http('GET', '/api/v1/orders/0'))[0] == 422
        expensive = await product(2147483647, '9999999999.99')
        assert (await place([{'product_id': expensive, 'quantity': 2147483647}]))[0] == 422
        assert await stock(expensive) == 2147483647 and await order_count() == count
        print('PASS: input/aggregate bounds and total overflow return 422 without persisting orders or changing stock.')

        async with sessions() as session:
            service = OrderService(session, PostgreSQLInventoryRepository(session), FailingOrderRepository(session))
            try:
                await service.create(OrderCreate.model_validate({'items': [{'product_id': first, 'quantity': 1}]}))
            except RuntimeError as error:
                assert str(error) == 'Injected order persistence failure'
            else:
                raise AssertionError('Persistence failure did not propagate')
        assert await stock(first) == 5 and await order_count() == count
        print('PASS: failure after order/item flush rolls back the order, its items, and stock together.')

        shared_a = await product(2, '1.00')
        shared_b = await product(2, '2.00')
        results = await asyncio.gather(
            place([{'product_id': shared_a, 'quantity': 1}, {'product_id': shared_b, 'quantity': 1}]),
            place([{'product_id': shared_b, 'quantity': 1}, {'product_id': shared_a, 'quantity': 1}]),
        )
        assert all(status == 201 for status, _ in results), results
        assert await stock(shared_a) == await stock(shared_b) == 0
        print('PASS: overlapping multi-item orders with reversed input both complete using sorted reservations.')
    finally:
        try:
            await cleanup_products(sessions, products, orders)
        finally:
            await engine.dispose()
    print('PASS: order validation complete; script-owned fixtures removed.')


if __name__ == '__main__':
    asyncio.run(main())
