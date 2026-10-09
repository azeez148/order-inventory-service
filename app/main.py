"""FastAPI application entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.health import router as health_router
from app.api.errors import register_exception_handlers
from app.api.products import router as products_router
from app.api.orders import router as orders_router
from app.core.config import Settings
from app.db.session import create_engine


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    settings = Settings.from_environment()
    engine = create_engine(settings)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        application.state.db_engine = engine
        application.state.session_factory = async_sessionmaker(
            engine, expire_on_commit=False
        )
        yield
    finally:
        await engine.dispose()


app = FastAPI(
    title="Order & Inventory Service",
    description="Shared order and inventory backend.",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(health_router)
app.include_router(products_router)
app.include_router(orders_router)
register_exception_handlers(app)
