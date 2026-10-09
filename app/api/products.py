"""Product HTTP endpoints and dependencies."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.repositories.postgres_products import PostgreSQLProductRepository
from app.schemas.products import ProductCreate, ProductPatch, ProductResponse, StockAdjustment
from app.services.products import ProductService
from app.services.search import ProductSearchService

router = APIRouter(
    prefix="/api/v1/products", tags=["products"],
    responses={503: {"description": "Database connection pool is temporarily exhausted"}},
)
ProductId = Annotated[int, Path(gt=0, le=9223372036854775807)]


async def get_product_service(session: Annotated[AsyncSession, Depends(get_session)]) -> ProductService:
    return ProductService(session, PostgreSQLProductRepository(session))


Service = Annotated[ProductService, Depends(get_product_service)]


async def get_search_service(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)],
) -> ProductSearchService:
    return ProductSearchService(
        request.app.state.search_provider, PostgreSQLProductRepository(session)
    )


SearchService = Annotated[ProductSearchService, Depends(get_search_service)]
SearchQuery = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
    Query(description="Search product name and description"),
]


@router.post("", response_model=ProductResponse, status_code=201)
async def create_product(payload: ProductCreate, service: Service) -> ProductResponse:
    return ProductResponse.model_validate(await service.create(payload))


@router.get(
    "/search", response_model=list[ProductResponse],
    responses={503: {"description": "Search or database connection capacity is unavailable"}},
)
async def search_products(
    q: SearchQuery, service: SearchService,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[ProductResponse]:
    return [ProductResponse.model_validate(product) for product in await service.search(q, limit)]


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
