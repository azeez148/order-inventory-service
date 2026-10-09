"""Asynchronous Meilisearch adapter with confirmed task completion."""

import asyncio
from dataclasses import asdict

import httpx

from app.search.provider import SearchProduct, SearchProviderError


class MeilisearchProvider:
    def __init__(
        self, client: httpx.AsyncClient, index_name: str = "products",
        task_timeout: float = 30,
    ) -> None:
        self._client = client
        self._index_path = f"/indexes/{index_name}"
        self._index_name = index_name
        self._task_timeout = task_timeout
        self._ready = False
        self._setup_lock = asyncio.Lock()

    async def _request(
        self, method: str, path: str, *, payload: dict | list | None = None,
        allow_missing: bool = False,
    ) -> httpx.Response:
        try:
            response = await self._client.request(method, path, json=payload)
            if response.status_code == 404 and not allow_missing and path.startswith(self._index_path):
                self._ready = False
            if not (allow_missing and response.status_code == 404):
                response.raise_for_status()
            return response
        except httpx.HTTPError:
            raise SearchProviderError("Search provider request failed") from None

    @staticmethod
    def _json(response: httpx.Response) -> dict:
        try:
            result = response.json()
        except ValueError:
            raise SearchProviderError("Search provider returned an invalid response") from None
        if not isinstance(result, dict):
            raise SearchProviderError("Search provider returned an invalid response")
        return result

    async def _wait_for_task(
        self, response: httpx.Response, *, allow_existing_index: bool = False,
    ) -> None:
        uid = self._json(response).get("taskUid")
        if type(uid) is not int or uid < 0:
            raise SearchProviderError("Search provider did not return a task identifier")
        try:
            async with asyncio.timeout(self._task_timeout):
                while True:
                    task = self._json(await self._request("GET", f"/tasks/{uid}"))
                    status = task.get("status")
                    if status == "succeeded":
                        return
                    if status in {"failed", "canceled"}:
                        error = task.get("error") or {}
                        if status == "failed" and allow_existing_index and isinstance(error, dict) and error.get("code") == "index_already_exists":
                            return
                        raise SearchProviderError("Search indexing task did not succeed")
                    if status not in {"enqueued", "processing"}:
                        raise SearchProviderError("Search provider returned an invalid task status")
                    await asyncio.sleep(0.05)
        except TimeoutError:
            raise SearchProviderError("Search indexing task timed out") from None

    async def _ensure_index(self) -> None:
        if self._ready:
            return
        async with self._setup_lock:
            if self._ready:
                return
            response = await self._request("GET", self._index_path, allow_missing=True)
            if response.status_code == 404:
                response = await self._request(
                    "POST", "/indexes", payload={"uid": self._index_name, "primaryKey": "id"},
                )
                await self._wait_for_task(response, allow_existing_index=True)
                response = await self._request("GET", self._index_path)
            if self._json(response).get("primaryKey") != "id":
                raise SearchProviderError("Search index has an incompatible primary key")
            response = await self._request(
                "PATCH", f"{self._index_path}/settings",
                payload={"searchableAttributes": ["name", "description"]},
            )
            await self._wait_for_task(response)
            self._ready = True

    async def search_products(self, query: str, limit: int = 20) -> list[int]:
        if not 1 <= limit <= 100:
            raise ValueError("Search limit must be between 1 and 100")
        await self._ensure_index()
        result = self._json(await self._request(
            "POST", f"{self._index_path}/search",
            payload={"q": query, "limit": limit, "attributesToRetrieve": ["id"]},
        ))
        hits = result.get("hits")
        if not isinstance(hits, list):
            raise SearchProviderError("Search provider returned invalid hits")
        ids = []
        for hit in hits:
            if not isinstance(hit, dict) or type(hit.get("id")) is not int or not 0 < hit["id"] <= 9223372036854775807:
                raise SearchProviderError("Search provider returned an invalid product ID")
            ids.append(hit["id"])
        return ids

    async def index_product(self, product: SearchProduct) -> None:
        if not 0 < product.id <= 9223372036854775807:
            raise ValueError("Product ID must be a positive BIGINT")
        await self._ensure_index()
        response = await self._request(
            "PUT", f"{self._index_path}/documents", payload=[asdict(product)],
        )
        await self._wait_for_task(response)

    async def remove_product(self, product_id: int) -> None:
        if not 0 < product_id <= 9223372036854775807:
            raise ValueError("Product ID must be a positive BIGINT")
        await self._ensure_index()
        response = await self._request("DELETE", f"{self._index_path}/documents/{product_id}")
        await self._wait_for_task(response)
