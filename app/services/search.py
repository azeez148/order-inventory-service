"""Text matching through a provider, authoritative values through persistence."""

from app.repositories.products import ProductRecord, ProductRepository
from app.search.provider import SearchProvider


class ProductSearchService:
    def __init__(self, provider: SearchProvider, products: ProductRepository) -> None:
        self._provider = provider
        self._products = products

    async def search(self, query: str, limit: int = 20) -> list[ProductRecord]:
        product_ids = await self._provider.search_products(query, limit)
        if not product_ids:
            return []
        products = await self._products.get_many(product_ids)
        by_id = {product.id: product for product in products}
        return [by_id[product_id] for product_id in product_ids if product_id in by_id]
