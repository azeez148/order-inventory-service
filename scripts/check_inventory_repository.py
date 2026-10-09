"""Check PostgreSQL inventory operations without exposing a business API.

Run: docker compose exec -T app python < scripts/check_inventory_repository.py
Only products created by this script are modified or deleted.
"""

import asyncio
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.db.session import create_engine
from app.repositories.inventory import InventoryRepository, StockReservation
from app.repositories.postgres_inventory import PostgreSQLInventoryRepository


async def main() -> None:
    engine = create_engine(Settings.from_environment())
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    product_ids = []
    try:
        async with sessions.begin() as session:
            for stock in (10, 1, 1):
                product_id = await session.scalar(
                    text("INSERT INTO products(name, price, stock) VALUES (:name, 19.99, :stock) RETURNING id"),
                    {"name": f"repository-check-{uuid4()}", "stock": stock},
                )
                product_ids.append(product_id)
        first, second, race_product = product_ids

        async with sessions() as session:
            repository: InventoryRepository = PostgreSQLInventoryRepository(session)
            try:
                await repository.reserve_stock(first, 1)
            except RuntimeError:
                pass
            else:
                raise AssertionError("Reservation allowed without a transaction")

        async with sessions() as session:
            async with session.begin():
                repository = PostgreSQLInventoryRepository(session)
                for quantity in (0, -1):
                    try:
                        await repository.reserve_stock(first, quantity)
                    except ValueError:
                        pass
                    else:
                        raise AssertionError("Nonpositive reservation accepted")
                reservation = await repository.reserve_stock(first, 3)
                assert reservation is not None
                assert reservation.product_id == first and reservation.remaining_stock == 7
                assert reservation.unit_price == Decimal('19.99')
                assert isinstance(reservation.unit_price, Decimal)
                assert await repository.reserve_stock(first, 8) is None
                assert await repository.product_exists(first)
                assert await repository.reserve_stock(9223372036854775807, 1) is None
                assert not await repository.product_exists(9223372036854775807)
                await session.rollback()
        print('PASS: caller-owned transaction, positive quantities, exact price, reservation, and failure distinction.')

        async with sessions.begin() as session:
            assert await session.scalar(text('SELECT stock FROM products WHERE id=:id'), {'id': first}) == 10
            assert await session.scalar(text('SELECT stock FROM products WHERE id=:id'), {'id': second}) == 1
        try:
            async with sessions.begin() as session:
                repository = PostgreSQLInventoryRepository(session)
                assert await repository.reserve_stock(first, 5) is not None
                assert await repository.reserve_stock(second, 5) is None
                raise RuntimeError('Expected transaction rollback')
        except RuntimeError as error:
            assert str(error) == 'Expected transaction rollback'
        async with sessions.begin() as session:
            assert await session.scalar(text('SELECT stock FROM products WHERE id=:id'), {'id': first}) == 10
            assert await session.scalar(text('SELECT stock FROM products WHERE id=:id'), {'id': second}) == 1
        print('PASS: rollback restores earlier reservations when a later item fails.')

        ready = asyncio.Event()

        async def reserve_one() -> StockReservation | None:
            await ready.wait()
            async with sessions.begin() as session:
                return await PostgreSQLInventoryRepository(session).reserve_stock(race_product, 1)

        attempts = [asyncio.create_task(reserve_one()) for _ in range(2)]
        ready.set()
        outcomes = await asyncio.gather(*attempts, return_exceptions=True)
        assert not any(isinstance(outcome, BaseException) for outcome in outcomes), outcomes
        assert sum(outcome is not None for outcome in outcomes) == 1
        async with sessions.begin() as session:
            assert await session.scalar(text('SELECT stock FROM products WHERE id=:id'), {'id': race_product}) == 0
        print('PASS: two concurrent database transactions reserve stock=1 with exactly one success and final stock=0.')
    finally:
        try:
            if product_ids:
                async with sessions.begin() as session:
                    await session.execute(text('DELETE FROM products WHERE id=ANY(:ids)'), {'ids': product_ids})
        finally:
            await engine.dispose()
    print('PASS: inventory repository checks complete; script-owned fixtures removed.')


if __name__ == '__main__':
    asyncio.run(main())
