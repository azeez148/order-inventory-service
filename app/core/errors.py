"""Business failures translated to HTTP only by the API layer."""


class ProductNotFound(Exception):
    def __init__(self, product_id: int) -> None:
        super().__init__(f"Product {product_id} not found")


class StockAdjustmentRejected(Exception):
    def __init__(self) -> None:
        super().__init__("Adjustment would put stock outside the allowed range")
