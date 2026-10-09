"""Translate business and pool failures without exposing infrastructure details."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import TimeoutError as PoolTimeout

from app.core.errors import ProductNotFound, StockAdjustmentRejected


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProductNotFound)
    async def product_not_found(request: Request, error: ProductNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(StockAdjustmentRejected)
    async def stock_conflict(request: Request, error: StockAdjustmentRejected) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(PoolTimeout)
    async def pool_timeout(request: Request, error: PoolTimeout) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": "Database connection capacity is temporarily exhausted"})
