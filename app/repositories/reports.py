"""Sales aggregate persistence contract and exact monetary results."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class TopProductRecord:
    product_id: int
    name: str
    items_sold: int
    revenue: Decimal


@dataclass(frozen=True)
class SalesSummaryRecord:
    total_orders: int
    total_revenue: Decimal
    items_sold: int
    average_order_value: Decimal
    top_products: tuple[TopProductRecord, ...]


class ReportingRepository(Protocol):
    async def sales_summary(self) -> SalesSummaryRecord:
        """Compute totals and top products from one consistent database statement."""
        ...
