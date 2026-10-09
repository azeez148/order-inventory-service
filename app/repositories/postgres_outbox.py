"""PostgreSQL event claiming and acknowledgement within caller-owned transactions."""

from dataclasses import dataclass

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SearchOutbox


@dataclass(frozen=True)
class PendingSearchEvent:
    id: int
    product_id: int
    event_type: str


class PostgreSQLSearchOutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_next(self) -> PendingSearchEvent | None:
        if not self._session.in_transaction():
            raise RuntimeError("Outbox claiming requires a caller-owned transaction")
        # One consumer per outbox table across processes. The negative table OID
        # is an application-reserved advisory key; isolated schemas get own keys.
        acquired = await self._session.scalar(text(
            "SELECT pg_try_advisory_xact_lock(-('search_outbox'::regclass::oid::bigint))"
        ))
        if not acquired:
            return None
        event = await self._session.scalar(
            select(SearchOutbox).where(SearchOutbox.processed_at.is_(None))
            .order_by(SearchOutbox.id).limit(1).with_for_update(skip_locked=True)
        )
        if event is None:
            return None
        return PendingSearchEvent(event.id, event.product_id, event.event_type)

    async def mark_processed(self, event_id: int) -> None:
        await self._session.execute(
            update(SearchOutbox).where(SearchOutbox.id == event_id)
            .values(processed_at=func.clock_timestamp())
        )
