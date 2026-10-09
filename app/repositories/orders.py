"""Order persistence contract and purchase snapshots."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class NewOrderItem:
    product_id: int
    quantity: int
    unit_price: Decimal


@dataclass(frozen=True)
class OrderItemRecord:
    id: int
    product_id: int
    quantity: int
    unit_price: Decimal


@dataclass(frozen=True)
class OrderRecord:
    id: int
    status: str
    total_amount: Decimal
    created_at: datetime
    items: tuple[OrderItemRecord, ...]


class OrderRepository(Protocol):
    async def create(self, total_amount: Decimal, items: list[NewOrderItem]) -> OrderRecord:
        """Persist order/items without committing the caller's transaction."""
        ...

    async def get(self, order_id: int) -> OrderRecord | None:
        """Retrieve the order and its stored purchase-price snapshots."""
        ...
