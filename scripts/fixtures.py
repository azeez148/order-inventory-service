"""Shared cleanup for validation-owned rows and search projections."""

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.search.factory import create_search_provider


async def cleanup_products(
    sessions: async_sessionmaker[AsyncSession], product_ids: list[int],
    order_ids: list[int] | None = None,
) -> None:
    # Delete intent before documents: an in-flight worker must finish/release its
    # event lock before this commit, so it cannot recreate the removed fixture.
    async with sessions.begin() as session:
        if order_ids:
            await session.execute(text("DELETE FROM order_items WHERE order_id=ANY(:ids)"), {"ids": order_ids})
            await session.execute(text("DELETE FROM orders WHERE id=ANY(:ids)"), {"ids": order_ids})
        if product_ids:
            await session.execute(text("DELETE FROM search_outbox WHERE product_id=ANY(:ids)"), {"ids": product_ids})
            await session.execute(text("DELETE FROM products WHERE id=ANY(:ids)"), {"ids": product_ids})
    if not product_ids:
        return
    settings = Settings.from_environment()
    key = settings.meilisearch_master_key.get_secret_value()
    async with httpx.AsyncClient(
        base_url=str(settings.meilisearch_url),
        headers={"Authorization": f"Bearer {key}"} if key else {},
        timeout=settings.search_http_timeout,
    ) as client:
        provider = create_search_provider(settings, client)
        for product_id in product_ids:
            await provider.remove_product(product_id)
