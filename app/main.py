"""FastAPI application entry point."""

from fastapi import FastAPI

from app.api.health import router as health_router

app = FastAPI(
    title="Order & Inventory Service",
    description="Shared order and inventory backend.",
    version="0.1.0",
)
app.include_router(health_router)
