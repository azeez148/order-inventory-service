"""Order request validation and immutable purchase responses."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OrderItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: int = Field(gt=0, le=9223372036854775807, strict=True)
    quantity: int = Field(gt=0, le=2147483647, strict=True)


class OrderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[OrderItemCreate] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_aggregate_quantities(self) -> "OrderCreate":
        quantities: dict[int, int] = {}
        for item in self.items:
            quantities[item.product_id] = quantities.get(item.product_id, 0) + item.quantity
            if quantities[item.product_id] > 2147483647:
                raise ValueError("Combined product quantity exceeds the supported range")
        return self


class OrderItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    quantity: int
    unit_price: Decimal


class OrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    total_amount: Decimal
    created_at: datetime
    items: list[OrderItemResponse]
