"""Product requests and responses, including database-compatible bounds."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

ProductName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
ProductDescription = Annotated[str, Field(max_length=10000)]
ProductPrice = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2, allow_inf_nan=False)]


class ProductCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ProductName
    description: ProductDescription = ""
    price: ProductPrice
    stock: int = Field(default=0, ge=0, le=2147483647, strict=True)


class ProductPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ProductName | None = None
    description: ProductDescription | None = None
    price: ProductPrice | None = None

    @model_validator(mode="after")
    def validate_fields(self) -> "ProductPatch":
        if not self.model_fields_set:
            raise ValueError("Provide at least one product field")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Product fields cannot be null")
        return self


class StockAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adjustment: int = Field(ge=-2147483647, le=2147483647, strict=True)

    @field_validator("adjustment")
    @classmethod
    def reject_zero(cls, value: int) -> int:
        if value == 0:
            raise ValueError("Adjustment must be nonzero")
        return value


class ProductResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    price: Decimal
    stock: int
    created_at: datetime
    updated_at: datetime
