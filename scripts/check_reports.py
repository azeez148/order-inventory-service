"""Check real reporting HTTP behavior in an isolated PostgreSQL schema.

Run: docker compose exec -T app python -m scripts.check_reports
Only the randomly named test schema is removed; public data is preserved.
"""

import asyncio
from decimal import Decimal
from uuid import uuid4

import httpx
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.models import Base, Order, OrderItem, Product
from app.db.session import create_engine
from app.main import app


async def main() -> None:
    settings = Settings.from_environment()
    schema = 'report_check_' + uuid4().hex
    admin = create_engine(settings)
    url = settings.database_url.get_secret_value()
    writes = create_async_engine(url, connect_args={'server_settings': {'search_path': schema}})
    reports = create_async_engine(
        url, pool_size=1, max_overflow=0, pool_timeout=0.1,
        connect_args={'server_settings': {
            'search_path': schema, 'default_transaction_read_only': 'on',
            'statement_timeout': '100',
        }},
    )
    sessions = async_sessionmaker(writes, expire_on_commit=False)
    app.state.reporting_session_factory = async_sessionmaker(reports, expire_on_commit=False)
    queries = []
    event.listen(reports.sync_engine, 'before_cursor_execute',
                 lambda conn, cursor, statement, parameters, context, many: queries.append(statement))
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA {schema}'))
        async with writes.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with reports.connect() as connection:
            assert await connection.scalar(text('SHOW transaction_read_only')) == 'on'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            async def summary():
                queries.clear()
                response = await client.get('/api/v1/reports/sales-summary')
                assert response.status_code == 200, response.text
                assert len(queries) == 1, queries
                assert reports.pool.checkedout() == 0
                return response.json()

            empty = await summary()
            assert empty['total_orders'] == empty['items_sold'] == 0
            assert Decimal(empty['total_revenue']) == Decimal(empty['average_order_value']) == 0
            assert empty['top_products'] == []
            print('PASS: empty report, one reporting SQL statement, and connection release.')

            async with sessions.begin() as session:
                products = [Product(name=f'fixture {i}', price=price, stock=100)
                            for i, price in enumerate([Decimal('10'), Decimal('0.10'), Decimal('20.01')])]
                session.add_all(products)
                await session.flush()
                for total, items in [(Decimal('20.30'), [(0, 2), (1, 3)]),
                                     (Decimal('30.01'), [(0, 1), (2, 1)])]:
                    order = Order(total_amount=total)
                    session.add(order)
                    await session.flush()
                    session.add_all([OrderItem(order_id=order.id, product_id=products[i].id,
                                              quantity=qty, unit_price=products[i].price)
                                     for i, qty in items])
            result = await summary()
            assert result['total_orders'] == 2 and result['items_sold'] == 7
            assert Decimal(result['total_revenue']) == Decimal('50.31')
            assert Decimal(result['average_order_value']) == Decimal('25.16')
            assert [p['product_id'] for p in result['top_products']] == [p.id for p in products]
            assert [Decimal(p['revenue']) for p in result['top_products']] == [Decimal('30'), Decimal('0.30'), Decimal('20.01')]
            async with sessions.begin() as session:
                product = await session.get(Product, products[0].id)
                product.price = Decimal('999')
            assert await summary() == result
            print('PASS: multi-item totals, quantities, half-cent rounding, ranked products, and historical prices.')

            async with sessions.begin() as session:
                extra = [Product(name=f'tie {i}', price=Decimal('1'), stock=10) for i in range(12)]
                session.add_all(extra)
                order = Order(total_amount=Decimal('12'))
                session.add(order)
                await session.flush()
                session.add_all([OrderItem(order_id=order.id, product_id=p.id, quantity=1, unit_price=p.price)
                                 for p in extra])
            result = await summary()
            assert result['total_orders'] == 3 and result['items_sold'] == 19
            assert Decimal(result['total_revenue']) == Decimal('62.31')
            assert Decimal(result['average_order_value']) == Decimal('20.77')
            assert [p['product_id'] for p in result['top_products']] == [p.id for p in products + extra[:7]]
            print('PASS: top ten cap and deterministic quantity/revenue/ID tie ordering.')

            async with writes.begin() as locker:
                await locker.execute(text('LOCK TABLE orders IN ACCESS EXCLUSIVE MODE'))
                response = await client.get('/api/v1/reports/sales-summary')
                assert response.status_code == 504, response.text
                assert response.json() == {'detail': 'Reporting query timed out'}
            assert reports.pool.checkedout() == 0
            assert await summary() == result
            print('PASS: actual aggregate exceeds test deadline under a table lock; HTTP 504 and recovery.')

            async with reports.connect():
                response = await client.get('/api/v1/reports/sales-summary')
                assert response.status_code == 503, response.text
                async with writes.connect() as connection:
                    assert await connection.scalar(text('SELECT 1')) == 1
            assert await summary() == result
            print('PASS: reporting pool exhaustion returns HTTP 503; OLTP remains available and reporting recovers.')
    finally:
        await reports.dispose()
        await writes.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS {schema} CASCADE'))
        await admin.dispose()


if __name__ == '__main__':
    asyncio.run(main())
