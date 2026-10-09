"""Product persistence contract and infrastructure-independent result."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class ProductRecord:
    id: int
    name: str
    description: str
    price: Decimal
    stock: int
    created_at: datetime
    updated_at: datetime


class ProductRepository(Protocol):
    async def create(self, name: str, description: str, price: Decimal, stock: int) -> ProductRecord: ...
    async def get(self, product_id: int, *, for_update: bool = False) -> ProductRecord | None: ...
    async def update(self, product_id: int, changes: dict[str, str | Decimal]) -> ProductRecord: ...
    async def adjust_stock(self, product_id: int, adjustment: int) -> ProductRecord | None: ...
    async def enqueue_search_update(self, product_id: int) -> None: ...
