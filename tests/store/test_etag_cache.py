from datetime import UTC, datetime

import httpx
import pytest
import respx

from harness.github.client import GitHub
from harness.store.db import Store
from harness.store.etag_cache import SqliteEtagCache


@pytest.fixture(params=["memory", "file"])
def store(request, tmp_path):
    if request.param == "memory":
        s = Store(":memory:", clock=lambda: datetime(2026, 10, 14, 16, 24, 5, tzinfo=UTC))
    else:
        s = Store(tmp_path / "test.db", clock=lambda: datetime(2026, 10, 14, 16, 24, 5, tzinfo=UTC))
    s.migrate()
    yield s
    s.close()


@pytest.mark.asyncio
async def test_etag_cache_with_github(store):
    cache = SqliteEtagCache(store)

    with respx.mock:
        route = respx.get("https://api.github.com/repos/owner/repo")

        # First request: 200 OK with ETag
        route.side_effect = [
            httpx.Response(200, json={"id": 42}, headers={"ETag": '"etag-1"'}),
            httpx.Response(304, headers={"ETag": '"etag-1"'}),
        ]

        async with GitHub("token", cache=cache) as gh:
            # First fetch populates cache
            repo1 = await gh.repo("owner/repo")
            assert repo1 == {"id": 42}

            # Check DB
            cached = cache.get("https://api.github.com/repos/owner/repo")
            assert cached is not None
            assert cached[0] == '"etag-1"'
            assert cached[1] == {"id": 42}

            # Second fetch gets 304 and reads from cache
            repo2 = await gh.repo("owner/repo")
            assert repo2 == {"id": 42}

            # Assert that the second request actually passed the cached ETag
            assert route.calls[1].request.headers["If-None-Match"] == '"etag-1"'
