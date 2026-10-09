"""Genuine SQL aggregates through an injected reporting session."""

from decimal import Decimal

from sqlalchemy import func, select, true
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ReportingTimedOut
from app.db.models import Order, OrderItem, Product
from app.repositories.reports import SalesSummaryRecord, TopProductRecord


class PostgreSQLReportingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def sales_summary(self) -> SalesSummaryRecord:
        order_metrics = select(
            func.count(Order.id).label("total_orders"),
            func.coalesce(func.sum(Order.total_amount), Decimal("0.00")).label("total_revenue"),
        ).cte("order_metrics")
        item_metrics = select(
            func.coalesce(func.sum(OrderItem.quantity), 0).label("items_sold"),
        ).select_from(OrderItem).join(Order).cte("item_metrics")
        quantity = func.sum(OrderItem.quantity)
        revenue = func.sum(OrderItem.quantity * OrderItem.unit_price)
        top = select(
            Product.id.label("product_id"), Product.name,
            quantity.label("product_items_sold"), revenue.label("product_revenue"),
        ).select_from(OrderItem).join(Order).join(Product)
        top = top.group_by(Product.id, Product.name).order_by(
            quantity.desc(), revenue.desc(), Product.id,
        ).limit(10).cte("top_products")
        average = func.coalesce(func.round(
            order_metrics.c.total_revenue / func.nullif(order_metrics.c.total_orders, 0), 2,
        ), Decimal("0.00"))
        query = select(
            order_metrics.c.total_orders, order_metrics.c.total_revenue,
            item_metrics.c.items_sold, average.label("average_order_value"),
            top.c.product_id, top.c.name, top.c.product_items_sold, top.c.product_revenue,
        ).select_from(
            order_metrics.join(item_metrics, true()).outerjoin(top, true())
        ).order_by(top.c.product_items_sold.desc(), top.c.product_revenue.desc(), top.c.product_id)
        try:
            rows = (await self._session.execute(query)).mappings().all()
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) == "57014":
                raise ReportingTimedOut() from None
            raise
        metrics = rows[0]
        return SalesSummaryRecord(
            total_orders=metrics["total_orders"], total_revenue=metrics["total_revenue"],
            items_sold=metrics["items_sold"], average_order_value=metrics["average_order_value"],
            top_products=tuple(
                TopProductRecord(
                    product_id=row["product_id"], name=row["name"],
                    items_sold=row["product_items_sold"], revenue=row["product_revenue"],
                )
                for row in rows if row["product_id"] is not None
            ),
        )
