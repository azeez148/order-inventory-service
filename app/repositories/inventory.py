"""Inventory persistence contract independent of SQLAlchemy and PostgreSQL."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class StockReservation:
    product_id: int
    remaining_stock: int
    unit_price: Decimal


class InventoryRepository(Protocol):
    async def reserve_stock(
        self, product_id: int, quantity: int
    ) -> StockReservation | None:
        """Reserve positive quantity in a caller-owned transaction.

        Return the current price and remaining stock on success, or None when
        the product is missing or stock is insufficient. Do not commit or roll
        back: the caller owns the complete order transaction.
        """
        ...

    async def product_exists(self, product_id: int) -> bool:
        """Distinguish a missing product from insufficient stock after failure."""
        ...
