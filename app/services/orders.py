"""Atomic order creation with normalized, deterministically ordered stock writes."""

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InsufficientStock, OrderNotFound, OrderTotalExceeded, ProductNotFound
from app.repositories.inventory import InventoryRepository
from app.repositories.orders import NewOrderItem, OrderRecord, OrderRepository
from app.schemas.orders import OrderCreate


class OrderService:
    def __init__(
        self, session: AsyncSession, inventory: InventoryRepository,
        orders: OrderRepository,
    ) -> None:
        self._session = session
        self._inventory = inventory
        self._orders = orders

    async def create(self, payload: OrderCreate) -> OrderRecord:
        quantities: dict[int, int] = {}
        for item in payload.items:
            quantities[item.product_id] = quantities.get(item.product_id, 0) + item.quantity

        async with self._session.begin():
            items: list[NewOrderItem] = []
            total = Decimal("0.00")
            for product_id, quantity in sorted(quantities.items()):
                reservation = await self._inventory.reserve_stock(product_id, quantity)
                if reservation is None:
                    if not await self._inventory.product_exists(product_id):
                        raise ProductNotFound(product_id)
                    raise InsufficientStock(product_id)
                items.append(NewOrderItem(product_id, quantity, reservation.unit_price))
                total += reservation.unit_price * quantity
                if total > Decimal("9999999999999999.99"):
                    raise OrderTotalExceeded()
            order = await self._orders.create(total, items)
        return order

    async def get(self, order_id: int) -> OrderRecord:
        order = await self._orders.get(order_id)
        if order is None:
            raise OrderNotFound(order_id)
        return order
