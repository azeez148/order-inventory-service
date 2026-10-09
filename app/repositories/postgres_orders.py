"""SQLAlchemy persistence for complete orders and their item snapshots."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, OrderItem
from app.repositories.orders import NewOrderItem, OrderItemRecord, OrderRecord


def order_record(order: Order, items: list[OrderItem]) -> OrderRecord:
    return OrderRecord(
        id=order.id, status=order.status, total_amount=order.total_amount,
        created_at=order.created_at,
        items=tuple(
            OrderItemRecord(
                id=item.id, product_id=item.product_id,
                quantity=item.quantity, unit_price=item.unit_price,
            )
            for item in items
        ),
    )


class PostgreSQLOrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, total_amount: Decimal, items: list[NewOrderItem]) -> OrderRecord:
        order = Order(total_amount=total_amount)
        self._session.add(order)
        await self._session.flush()
        rows = [
            OrderItem(
                order_id=order.id, product_id=item.product_id,
                quantity=item.quantity, unit_price=item.unit_price,
            )
            for item in items
        ]
        self._session.add_all(rows)
        await self._session.flush()
        return order_record(order, rows)

    async def get(self, order_id: int) -> OrderRecord | None:
        order = await self._session.get(Order, order_id)
        if order is None:
            return None
        items = list((await self._session.scalars(
            select(OrderItem).where(OrderItem.order_id == order_id)
            .order_by(OrderItem.product_id)
        )).all())
        return order_record(order, items)
