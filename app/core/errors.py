"""Business failures translated to HTTP only by the API layer."""


class ProductNotFound(Exception):
    def __init__(self, product_id: int) -> None:
        super().__init__(f"Product {product_id} not found")


class StockAdjustmentRejected(Exception):
    def __init__(self) -> None:
        super().__init__("Adjustment would put stock outside the allowed range")


class InsufficientStock(Exception):
    def __init__(self, product_id: int) -> None:
        super().__init__(f"Insufficient stock for product {product_id}")


class OrderNotFound(Exception):
    def __init__(self, order_id: int) -> None:
        super().__init__(f"Order {order_id} not found")


class OrderTotalExceeded(Exception):
    def __init__(self) -> None:
        super().__init__("Order total exceeds the supported monetary range")


class ReportingTimedOut(Exception):
    def __init__(self) -> None:
        super().__init__("Reporting query timed out")
