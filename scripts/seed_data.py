"""Generate a persistent assessment dataset and measure the real sales report.

Run: docker compose exec -T app python -m scripts.seed_data
Requires the running app/outbox worker. Reuses the marked dataset on repeat runs.
"""

import asyncio
import json
from decimal import Decimal
from statistics import median
from time import perf_counter

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.db.session import create_engine, create_reporting_engine
from app.repositories.postgres_reports import PostgreSQLReportingRepository

PRODUCTS = 3000
ORDERS = 30000
MARKER = 'assessment-seed-v1: '

# Temporary mappings allocate identity sequence values explicitly; concurrent API
# inserts cannot change the association between generated ordinals and row IDs.
STATEMENTS = [
    """CREATE TEMP TABLE seed_products ON COMMIT DROP AS
       SELECT n, nextval(pg_get_serial_sequence('products', 'id')) AS id,
              (5 + (n % 2000)::numeric / 100)::numeric(12,2) AS price
       FROM generate_series(1, :products) AS n""",
    """INSERT INTO products (id, name, description, price, stock)
       OVERRIDING SYSTEM VALUE
       SELECT id, (ARRAY['Keyboard','Mouse','Monitor','Headphones','Cable',
                        'Camera','Speaker','Desk','Charger','Notebook'])[1 + n % 10]
                  || ' Model ' || n,
              :marker || 'Office electronics and accessories, model ' || n,
              price, 100000 FROM seed_products""",
    """CREATE TEMP TABLE seed_orders ON COMMIT DROP AS
       SELECT n, nextval(pg_get_serial_sequence('orders', 'id')) AS id
       FROM generate_series(1, :orders) AS n""",
    """CREATE TEMP TABLE seed_items ON COMMIT DROP AS
       SELECT o.id AS order_id, p.id AS product_id, 1 + (o.n + slot) % 3 AS quantity,
              p.price AS unit_price FROM seed_orders o
       CROSS JOIN generate_series(0, 2) AS slot
       JOIN seed_products p ON p.n = 1 + ((o.n - 1) * 3 + slot) % :products""",
    """INSERT INTO orders (id, total_amount, created_at) OVERRIDING SYSTEM VALUE
       SELECT o.id, SUM(i.quantity * i.unit_price),
              TIMESTAMPTZ '2025-01-01 00:00:00+00' + o.n * INTERVAL '1 second'
       FROM seed_orders o JOIN seed_items i ON i.order_id = o.id GROUP BY o.id, o.n""",
    """INSERT INTO order_items (order_id, product_id, quantity, unit_price)
       SELECT order_id, product_id, quantity, unit_price FROM seed_items""",
    """UPDATE products p SET stock = p.stock - sold.quantity
       FROM (SELECT product_id, SUM(quantity)::integer AS quantity
             FROM seed_items GROUP BY product_id) sold WHERE p.id = sold.product_id""",
    """INSERT INTO search_outbox (product_id, event_type)
       SELECT id, 'upsert' FROM seed_products""",
]

SEED_ORDERS = """SELECT DISTINCT o.id FROM orders o JOIN order_items i ON i.order_id=o.id
    JOIN products p ON p.id=i.product_id WHERE p.description LIKE :pattern
    AND o.created_at > TIMESTAMPTZ '2025-01-01 00:00:00+00'
    AND o.created_at <= TIMESTAMPTZ '2025-01-01 00:00:00+00' + :orders * INTERVAL '1 second'"""


async def main() -> None:
    settings = Settings.from_environment()
    engine = create_engine(settings)
    reporting = create_reporting_engine(settings)
    params = {'products': PRODUCTS, 'orders': ORDERS, 'marker': MARKER, 'pattern': MARKER + '%'}
    try:
        started = perf_counter()
        async with engine.begin() as connection:
            await connection.execute(text('SELECT pg_advisory_xact_lock(715003015)'))
            existing = await connection.scalar(text(
                'SELECT COUNT(*) FROM products WHERE description LIKE :pattern'), params)
            if existing == 0:
                for statement in STATEMENTS:
                    await connection.execute(text(statement), params)
                print(f'Created dataset in {perf_counter() - started:.3f}s.', flush=True)
            elif existing == PRODUCTS:
                print('Reusing existing marked dataset.', flush=True)
            else:
                raise RuntimeError('Seed marker has unexpected product count; existing data was preserved')

        # Check the real schema and generated order arithmetic independently of
        # the report implementation. New API orders have different timestamps.
        async with engine.connect() as connection:
            seed_counts = (await connection.execute(text(f"""
                WITH seeded AS ({SEED_ORDERS})
                SELECT (SELECT COUNT(*) FROM products WHERE description LIKE :pattern) AS products,
                       (SELECT COUNT(*) FROM seeded) AS orders,
                       COUNT(i.id) AS order_items, COALESCE(SUM(i.quantity),0) AS units,
                       COALESCE(SUM(i.quantity*i.unit_price),0) AS revenue
                FROM order_items i JOIN seeded s ON s.id=i.order_id"""), params)).mappings().one()
            assert seed_counts['products'] == PRODUCTS and seed_counts['orders'] == ORDERS
            assert seed_counts['order_items'] == ORDERS * 3
            assert seed_counts['units'] == 180000
            assert seed_counts['revenue'] == Decimal('2399700.00')
            inconsistent = await connection.scalar(text(f"""
                WITH seeded AS ({SEED_ORDERS})
                SELECT COUNT(*) FROM (SELECT o.id FROM orders o
                JOIN seeded s ON s.id=o.id JOIN order_items i ON i.order_id=o.id
                GROUP BY o.id, o.total_amount HAVING o.total_amount <> SUM(i.quantity*i.unit_price)) bad
                """), params)
            assert inconsistent == 0
            negative = await connection.scalar(text(
                'SELECT COUNT(*) FROM products WHERE description LIKE :pattern AND stock < 0'), params)
            assert negative == 0
        print('Validated seed counts: ' + json.dumps(dict(seed_counts), default=str), flush=True)

        # Events remain durable if indexing fails or this process is interrupted.
        # The normal application worker owns all search synchronization.
        async with asyncio.timeout(900):
            last_progress = 0.0
            while True:
                async with engine.connect() as connection:
                    pending = await connection.scalar(text("""
                        SELECT COUNT(*) FROM search_outbox e JOIN products p ON p.id=e.product_id
                        WHERE p.description LIKE :pattern AND e.processed_at IS NULL"""), params)
                if pending == 0:
                    break
                if perf_counter() - last_progress >= 30:
                    print(f'Waiting for search outbox: {pending} pending seed events.', flush=True)
                    last_progress = perf_counter()
                await asyncio.sleep(2)
        print('PASS: all seed outbox events acknowledged by the application worker.', flush=True)

        async with engine.begin() as connection:
            for table in ['products', 'orders', 'order_items']:
                await connection.execute(text(f'ANALYZE {table}'))
        sessions = async_sessionmaker(reporting, expire_on_commit=False)
        timings = []
        for _ in range(6):
            async with sessions() as session:
                started = perf_counter()
                report = await PostgreSQLReportingRepository(session).sales_summary()
                timings.append((perf_counter() - started) * 1000)
        # First execution includes pool startup; record it separately.
        async with httpx.AsyncClient(base_url='http://localhost:8000', timeout=30) as client:
            started = perf_counter()
            response = await client.get('/api/v1/reports/sales-summary')
            http_ms = (perf_counter() - started) * 1000
            response.raise_for_status()
            result = response.json()
            assert result['total_orders'] >= ORDERS
            assert result['items_sold'] >= seed_counts['units']
            assert Decimal(result['total_revenue']) >= seed_counts['revenue']
            response = await client.get('/api/v1/products/search', params={'q': 'Keyboard', 'limit': 20})
            response.raise_for_status()
            assert any(p['description'].startswith(MARKER) for p in response.json())
        async with engine.connect() as connection:
            actual = {table: await connection.scalar(text(f'SELECT COUNT(*) FROM {table}'))
                      for table in ['products', 'orders', 'order_items']}
        print(json.dumps({
            'database_rows': actual, 'seed_rows': dict(seed_counts),
            'report_total_orders': report.total_orders, 'report_total_revenue': str(report.total_revenue),
            'report_items_sold': report.items_sold,
            'first_report_ms': round(timings[0], 2),
            'warm_report_ms': [round(value, 2) for value in timings[1:]],
            'warm_report_median_ms': round(median(timings[1:]), 2),
            'http_report_ms': round(http_ms, 2), 'seed_pending_outbox': pending,
        }, default=str, indent=2), flush=True)
        print('PASS: generated totals, nonnegative stock, live search, and reporting.', flush=True)
    finally:
        await reporting.dispose()
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
