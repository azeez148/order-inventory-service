"""Product HTTP endpoints and dependencies."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.repositories.postgres_products import PostgreSQLProductRepository
from app.schemas.products import ProductCreate, ProductPatch, ProductResponse, StockAdjustment
from app.services.products import ProductService

router = APIRouter(
    prefix="/api/v1/products", tags=["products"],
    responses={503: {"description": "Database connection pool is temporarily exhausted"}},
)
ProductId = Annotated[int, Path(gt=0, le=9223372036854775807)]


async def get_product_service(session: Annotated[AsyncSession, Depends(get_session)]) -> ProductService:
    return ProductService(session, PostgreSQLProductRepository(session))


Service = Annotated[ProductService, Depends(get_product_service)]


@router.post("", response_model=ProductResponse, status_code=201)
async def create_product(payload: ProductCreate, service: Service) -> ProductResponse:
    return ProductResponse.model_validate(await service.create(payload))


@router.get("/{product_id}", response_model=ProductResponse, responses={404: {"description": "Product not found"}})
async def get_product(product_id: ProductId, service: Service) -> ProductResponse:
    return ProductResponse.model_validate(await service.get(product_id))


@router.patch("/{product_id}", response_model=ProductResponse, responses={404: {"description": "Product not found"}})
async def update_product(product_id: ProductId, payload: ProductPatch, service: Service) -> ProductResponse:
    return ProductResponse.model_validate(await service.update(product_id, payload))


@router.post(
    "/{product_id}/stock", response_model=ProductResponse,
    responses={404: {"description": "Product not found"}, 409: {"description": "Stock adjustment rejected"}},
)
async def adjust_stock(product_id: ProductId, payload: StockAdjustment, service: Service) -> ProductResponse:
    return ProductResponse.model_validate(await service.adjust_stock(product_id, payload.adjustment))
