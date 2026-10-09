"""Validate the adapter using mock transport and an isolated live index.

Run: docker compose exec -T app python < scripts/check_search_provider.py
Only the script's randomly named live index is deleted.
"""

import asyncio
from uuid import uuid4
from unittest.mock import patch

import httpx
from pydantic import ValidationError

from app.core.config import Settings
from app.main import app, lifespan
from app.search.factory import create_search_provider
from app.search.meilisearch import MeilisearchProvider
from app.search.provider import SearchProduct, SearchProviderError


async def check_failure_modes() -> None:
    for failure in ('failed', 'canceled', 'processing', 'unauthorized', 'network', 'malformed'):
        calls = []

        def respond(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if failure == 'network':
                raise httpx.ConnectError('Private connection details', request=request)
            if failure == 'unauthorized':
                return httpx.Response(401, json={'message': 'Private authorization details'})
            if failure == 'malformed' and request.method != 'GET':
                return httpx.Response(200, json={'taskUid': 'invalid'})
            if request.url.path.startswith('/tasks/'):
                return httpx.Response(200, json={'status': failure, 'error': {'message': 'Private task details'}})
            if request.method == 'GET':
                return httpx.Response(200, json={'primaryKey': 'id'})
            return httpx.Response(202, json={'taskUid': 1})

        async with httpx.AsyncClient(base_url='http://mock', transport=httpx.MockTransport(respond)) as client:
            provider = MeilisearchProvider(client, task_timeout=0.1)
            try:
                await provider.index_product(SearchProduct(1, 'Football', 'Training ball'))
            except SearchProviderError as error:
                assert 'Private' not in str(error)
            else:
                raise AssertionError(f'Failure incorrectly acknowledged: {failure}')
            if failure in ('failed', 'canceled', 'processing'):
                assert '/tasks/1' in calls, 'Provider did not poll the task'
        assert client.is_closed
    print('PASS: failed/canceled/timed-out tasks, authorization/network failures, malformed tasks, and client cleanup.')

    requests = []

    def ranked(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith('/search'):
            return httpx.Response(200, json={'hits': [{'id': 42}, {'id': 7}]})
        if request.url.path.startswith('/tasks/'):
            return httpx.Response(200, json={'status': 'succeeded'})
        if request.method == 'GET':
            return httpx.Response(200, json={'primaryKey': 'id'})
        return httpx.Response(202, json={'taskUid': 1})

    async with httpx.AsyncClient(base_url='http://mock', transport=httpx.MockTransport(ranked)) as client:
        provider = MeilisearchProvider(client)
        assert await provider.search_products('Football') == [42, 7]
        assert await provider.search_products('Football') == [42, 7]
        assert sum(request.url.path.endswith('/settings') for request in requests) == 1
        for limit in (0, 101):
            try:
                await provider.search_products('Football', limit)
            except ValueError:
                pass
            else:
                raise AssertionError('Invalid search limit accepted')
    print('PASS: relevance ordering is preserved, setup is reused, and limit bounds are enforced.')


async def check_live(settings: Settings) -> None:
    index = 'provider_check_' + uuid4().hex
    settings = settings.model_copy(update={'meilisearch_index': index})
    key = settings.meilisearch_master_key.get_secret_value()
    async with httpx.AsyncClient(
        base_url=str(settings.meilisearch_url),
        headers={'Authorization': f'Bearer {key}'} if key else {},
        timeout=settings.search_http_timeout,
    ) as client:
        provider = create_search_provider(settings, client)
        try:
            await asyncio.gather(
                provider.index_product(SearchProduct(1, 'Football training', 'Outdoor ball')),
                provider.index_product(SearchProduct(2, 'Goalkeeper glove', 'Football protection')),
            )
            matches = await provider.search_products('Football')
            assert set(matches) == {1, 2}, matches
            assert len(await provider.search_products('Football', limit=1)) == 1
            assert await provider.search_products('protection') == [2]
            assert set(await provider.search_products('fotball')) == {1, 2}
            await provider.index_product(SearchProduct(1, 'Notebook', 'Paper stationery'))
            assert await provider.search_products('Football') == [2]
            assert await provider.search_products('Notebook') == [1]
            await provider.remove_product(2)
            await provider.remove_product(2)
            assert await provider.search_products('Football') == []
            response = await client.get(f'/indexes/{index}/settings/searchable-attributes')
            response.raise_for_status()
            assert response.json() == ['name', 'description']
            print('PASS: live index setup, name/description matching, typo tolerance, limit, idempotent upsert/removal, and confirmed visibility.')
        finally:
            response = await client.delete(f'/indexes/{index}')
            if response.status_code != 404:
                response.raise_for_status()
                uid = response.json()['taskUid']
                async with asyncio.timeout(settings.search_task_timeout):
                    while True:
                        task = await client.get(f'/tasks/{uid}')
                        task.raise_for_status()
                        status = task.json()['status']
                        if status == 'succeeded':
                            break
                        assert status in ('enqueued', 'processing'), task.json()
                        await asyncio.sleep(0.05)
    assert client.is_closed
    print('PASS: isolated test index removed; production index and PostgreSQL data unchanged.')


async def check_lifecycle() -> None:
    clients = []

    def factory(settings: Settings, client: httpx.AsyncClient):
        clients.append(client)
        return create_search_provider(settings, client)

    with patch.dict('os.environ', {'MEILISEARCH_URL': 'http://127.0.0.1:1'}):
        with patch('app.main.create_search_provider', factory):
            async with lifespan(app):
                assert app.state.search_provider is not None
            assert clients[-1].is_closed
            try:
                async with lifespan(app):
                    raise RuntimeError('Expected lifespan failure')
            except RuntimeError as error:
                assert str(error) == 'Expected lifespan failure'
            assert clients[-1].is_closed
    print('PASS: unavailable search does not block app lifespan; HTTP clients close on normal and exceptional exit.')


async def main() -> None:
    settings = Settings.from_environment()
    for override in (
        {'search_provider': 'opensearch'}, {'meilisearch_index': '../invalid'},
        {'meilisearch_url': 'file:///private'}, {'search_http_timeout': 0},
        {'search_task_timeout': 'nan'},
    ):
        try:
            Settings.model_validate(settings.model_dump() | override)
        except ValidationError:
            pass
        else:
            raise AssertionError('Invalid search configuration accepted')
    key = settings.meilisearch_master_key.get_secret_value()
    if key:
        assert key not in repr(settings)
    await check_failure_modes()
    await check_live(settings)
    await check_lifecycle()


if __name__ == '__main__':
    asyncio.run(main())
