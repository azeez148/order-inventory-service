"""Select the configured provider while lifecycle owns its HTTP client."""

import httpx

from app.core.config import Settings
from app.search.meilisearch import MeilisearchProvider
from app.search.provider import SearchProvider


def create_search_provider(settings: Settings, client: httpx.AsyncClient) -> SearchProvider:
    if settings.search_provider != "meilisearch":
        raise ValueError("Unsupported search provider")
    return MeilisearchProvider(client, settings.meilisearch_index, settings.search_task_timeout)
