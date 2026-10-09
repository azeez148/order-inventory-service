"""Durable search projection worker without an additional queue or container."""

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.postgres_outbox import PostgreSQLSearchOutboxRepository
from app.repositories.postgres_products import PostgreSQLProductRepository
from app.search.provider import SearchProduct, SearchProvider, SearchProviderError

logger = logging.getLogger(__name__)


class SearchOutboxWorker:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], provider: SearchProvider,
        poll_interval: float = 1,
    ) -> None:
        self._sessions = sessions
        self._provider = provider
        self._poll_interval = poll_interval

    async def process_once(self) -> bool:
        async with self._sessions.begin() as session:
            outbox = PostgreSQLSearchOutboxRepository(session)
            event = await outbox.claim_next()
            if event is None:
                return False
            if event.event_type != "upsert":
                raise SearchProviderError("Unsupported search outbox event type")
            product = await PostgreSQLProductRepository(session).get(event.product_id)
            if product is None:
                await self._provider.remove_product(event.product_id)
            else:
                await self._provider.index_product(
                    SearchProduct(product.id, product.name, product.description)
                )
            await outbox.mark_processed(event.id)
        return True

    async def run(self) -> None:
        while True:
            try:
                if await self.process_once():
                    continue
            except Exception as error:
                # Rollback has already released locks. Avoid logging private
                # credentials/payloads from network or database exception text.
                logger.warning("Search outbox processing failed; pending event will retry (%s)", type(error).__name__)
            await asyncio.sleep(self._poll_interval)
