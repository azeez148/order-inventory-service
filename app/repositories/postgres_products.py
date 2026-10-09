"""SQLAlchemy product persistence and atomic PostgreSQL stock adjustment."""

from decimal import Decimal

from sqlalchemy import BigInteger, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Product, SearchOutbox
from app.repositories.products import ProductRecord


def product_record(product: Product) -> ProductRecord:
    return ProductRecord(
        id=product.id, name=product.name, description=product.description,
        price=product.price, stock=product.stock,
        created_at=product.created_at, updated_at=product.updated_at,
    )


class PostgreSQLProductRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, name: str, description: str, price: Decimal, stock: int) -> ProductRecord:
        product = Product(name=name, description=description, price=price, stock=stock)
        self._session.add(product)
        await self._session.flush()
        return product_record(product)

    async def get(self, product_id: int, *, for_update: bool = False) -> ProductRecord | None:
        query = select(Product).where(Product.id == product_id)
        if for_update:
            query = query.with_for_update()
        product = await self._session.scalar(query)
        return product_record(product) if product is not None else None

    async def update(self, product_id: int, changes: dict[str, str | Decimal]) -> ProductRecord:
        product = (await self._session.execute(
            update(Product).where(Product.id == product_id)
            .values(**changes, updated_at=func.now()).returning(Product)
            .execution_options(populate_existing=True)
        )).scalar_one()
        return product_record(product)

    async def adjust_stock(self, product_id: int, adjustment: int) -> ProductRecord | None:
        new_stock = cast(Product.stock, BigInteger) + adjustment
        product = (await self._session.execute(
            update(Product)
            .where(Product.id == product_id, new_stock.between(0, 2147483647))
            .values(stock=new_stock, updated_at=func.now()).returning(Product)
        )).scalar_one_or_none()
        return product_record(product) if product is not None else None

    async def enqueue_search_update(self, product_id: int) -> None:
        self._session.add(SearchOutbox(product_id=product_id, event_type="upsert"))
        await self._session.flush()
