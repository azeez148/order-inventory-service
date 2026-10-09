"""PostgreSQL-specific atomic inventory operations."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.inventory import StockReservation


class PostgreSQLInventoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def reserve_stock(
        self, product_id: int, quantity: int
    ) -> StockReservation | None:
        if product_id <= 0 or quantity <= 0:
            raise ValueError("Product ID and quantity must be positive")
        if not self._session.in_transaction():
            raise RuntimeError("Stock reservation requires a caller-owned transaction")

        result = await self._session.execute(
            text(
                """
                UPDATE products
                SET stock = stock - :quantity, updated_at = CURRENT_TIMESTAMP
                WHERE id = :product_id AND stock >= :quantity
                RETURNING id, stock, price
                """
            ),
            {"product_id": product_id, "quantity": quantity},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return StockReservation(
            product_id=row["id"], remaining_stock=row["stock"], unit_price=row["price"]
        )

    async def product_exists(self, product_id: int) -> bool:
        return bool(
            await self._session.scalar(
                text("SELECT EXISTS (SELECT 1 FROM products WHERE id = :product_id)"),
                {"product_id": product_id},
            )
        )
