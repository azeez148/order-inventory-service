"""Provider-independent product text-search behavior."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SearchProduct:
    id: int
    name: str
    description: str


class SearchProviderError(Exception):
    """Search failed; indexing intent must remain retryable."""


class SearchProvider(Protocol):
    async def search_products(self, query: str, limit: int = 20) -> list[int]:
        """Return matching IDs in relevance order, not authoritative stock/prices."""
        ...

    async def index_product(self, product: SearchProduct) -> None:
        """Upsert searchable fields; return only after confirmed indexing success."""
        ...

    async def remove_product(self, product_id: int) -> None:
        """Remove a projection; return only after confirmed indexing success."""
        ...
