"""Order HTTP routes; stock SQL remains behind repository contracts."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.repositories.postgres_inventory import PostgreSQLInventoryRepository
from app.repositories.postgres_orders import PostgreSQLOrderRepository
from app.schemas.orders import OrderCreate, OrderResponse
from app.services.orders import OrderService

router = APIRouter(
    prefix="/api/v1/orders", tags=["orders"],
    responses={503: {"description": "Database connection pool is temporarily exhausted"}},
)
OrderId = Annotated[int, Path(gt=0, le=9223372036854775807)]


async def get_order_service(session: Annotated[AsyncSession, Depends(get_session)]) -> OrderService:
    return OrderService(
        session, PostgreSQLInventoryRepository(session), PostgreSQLOrderRepository(session)
    )


Service = Annotated[OrderService, Depends(get_order_service)]


@router.post(
    "", response_model=OrderResponse, status_code=201,
    responses={404: {"description": "Product not found"}, 409: {"description": "Insufficient stock"}},
)
async def create_order(payload: OrderCreate, service: Service) -> OrderResponse:
    return OrderResponse.model_validate(await service.create(payload))


@router.get(
    "/{order_id}", response_model=OrderResponse,
    responses={404: {"description": "Order not found"}},
)
async def get_order(order_id: OrderId, service: Service) -> OrderResponse:
    return OrderResponse.model_validate(await service.get(order_id))
