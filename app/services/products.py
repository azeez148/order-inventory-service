"""Product transactions and search synchronization intent."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ProductNotFound, StockAdjustmentRejected
from app.repositories.products import ProductRecord, ProductRepository
from app.schemas.products import ProductCreate, ProductPatch


class ProductService:
    def __init__(self, session: AsyncSession, repository: ProductRepository) -> None:
        self._session = session
        self._repository = repository

    async def create(self, payload: ProductCreate) -> ProductRecord:
        async with self._session.begin():
            product = await self._repository.create(
                payload.name, payload.description, payload.price, payload.stock
            )
            await self._repository.enqueue_search_update(product.id)
        return product

    async def get(self, product_id: int) -> ProductRecord:
        product = await self._repository.get(product_id)
        if product is None:
            raise ProductNotFound(product_id)
        return product

    async def update(self, product_id: int, payload: ProductPatch) -> ProductRecord:
        async with self._session.begin():
            current = await self._repository.get(product_id, for_update=True)
            if current is None:
                raise ProductNotFound(product_id)
            changes = {
                key: value for key, value in payload.model_dump(exclude_unset=True).items()
                if value != getattr(current, key)
            }
            if not changes:
                return current
            product = await self._repository.update(product_id, changes)
            if {"name", "description"}.intersection(changes):
                await self._repository.enqueue_search_update(product_id)
        return product

    async def adjust_stock(self, product_id: int, adjustment: int) -> ProductRecord:
        async with self._session.begin():
            product = await self._repository.adjust_stock(product_id, adjustment)
            if product is None:
                if await self._repository.get(product_id) is None:
                    raise ProductNotFound(product_id)
                raise StockAdjustmentRejected()
        return product
