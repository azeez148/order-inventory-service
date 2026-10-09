"""Reporting responses preserve exact decimal monetary values."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class TopProductResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    product_id: int
    name: str
    items_sold: int
    revenue: Decimal


class SalesSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    total_orders: int
    total_revenue: Decimal
    items_sold: int
    average_order_value: Decimal
    top_products: list[TopProductResponse]
